"""Issue 375: execute the shipped KCL, then inspect actual dashboard JSON."""
import copy
import json
import unittest

import tempfile
import subprocess
from pathlib import Path
import yaml
from render_harness import PIPELINE_COMPOSITION, make_oxr, render, manifests_of_kind, active_namespace_requirement


def context(app="alpha", services=None):
    xr = make_oxr(appName=app, services=services or [])
    xr["metadata"]["uid"] = "uid-" + app
    ns = active_namespace_requirement(app)
    return {"oxr": xr, "requiredResources": {
        "dashboardApplications": [{"Resource": xr}], "targetNamespace": ns,
    }}


def dashboard(items):
    cms = [m for m in manifests_of_kind(items, "ConfigMap")
           if m.get("metadata", {}).get("labels", {}).get("grafana_dashboard") == "1"]
    if len(cms) != 1:
        raise AssertionError(f"expected one app dashboard, got {len(cms)}")
    cm = cms[0]
    return cm, json.loads(next(iter(cm["data"].values())))


class AppDashboardTests(unittest.TestCase):
    def test_template_update_preserves_identity_and_changes_payload_hash(self):
        params = context()
        original = render(params)
        cm, _ = dashboard(original)
        with tempfile.TemporaryDirectory() as d:
            candidate = Path(d) / 'pipeline.yaml'
            candidate.write_text(Path(PIPELINE_COMPOSITION).read_text().replace('Telemetry confidence', 'Telemetry confidence updated'))
            updated, _ = dashboard(render(params, str(candidate)))
        self.assertEqual(cm['metadata']['name'], updated['metadata']['name'])
        self.assertEqual(cm['metadata']['annotations']['platform.digiorg.io/dashboard-owner'], updated['metadata']['annotations']['platform.digiorg.io/dashboard-owner'])
        self.assertNotEqual(cm['metadata']['annotations']['platform.digiorg.io/dashboard-sha256'], updated['metadata']['annotations']['platform.digiorg.io/dashboard-sha256'])
        for item in original:
            manifest = item.get('spec', {}).get('forProvider', {}).get('manifest', {})
            self.assertNotIn(manifest.get('kind'), ('Grafana', 'Elasticsearch', 'OpenSearchCluster'))

    def test_promtool_executes_literal_scope_and_missing_limits(self):
        _, doc = dashboard(render(context()))
        expressions = [t['expr'].replace('${pod:regex}', '.*') for p in doc['panels'] for t in p['targets'] if 'expr' in t]
        memory = next(p for p in doc['panels'] if p['title'] == 'Memory working set')['targets'][0]['expr'].replace('${pod:regex}', '.*')
        limits = next(p for p in doc['panels'] if p['title'] == 'CPU requests / limits')['targets'][1]['expr'].replace('${pod:regex}', '.*')
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            directory.chmod(0o755)
            (directory / 'rules.yaml').write_text(yaml.safe_dump({'groups': [{'name': 'parsed-canonical-panels', 'rules': [{'record': 'fixture:panel_' + str(i), 'expr': e} for i, e in enumerate(expressions)]}]}))
            samples = [{'series': 'container_memory_working_set_bytes{namespace="' + ns + '",pod="api-a",container="api",job="kubelet",metrics_path="/metrics/cadvisor"}', 'values': value} for ns, value in [('alpha', '123+0x10'), ('bravo', '456+0x10')]]
            samples.append({'series': 'kube_pod_container_resource_requests{namespace="alpha",pod="api-a",container="api",resource="cpu",unit="core"}', 'values': '1+0x10'})
            test = {'rule_files': ['rules.yaml'], 'evaluation_interval': '30s', 'tests': [{'interval': '30s', 'input_series': samples,
                'promql_expr_test': [{'expr': memory, 'eval_time': '5m', 'exp_samples': [{'labels': '{namespace="alpha",pod="api-a",container="api"}', 'value': 123}]},
                                     {'expr': limits, 'eval_time': '5m', 'exp_samples': []}]}]}
            (directory / 'tests.yaml').write_text(yaml.safe_dump(test))
            image = 'quay.io/prometheus/prometheus:v3.13.1-distroless@sha256:214f8427c8fba80c327bb94a75feb802ae12f2d6ca30812aa6e7d22f09bbea80'
            result = subprocess.run(['docker', 'run', '--rm', '--network=none', '--entrypoint', '/bin/promtool', '-v', d + ':/fixture:ro', '-w', '/fixture', image, 'test', 'rules', 'tests.yaml'], capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_one_literal_dashboard_before_first_workload(self):
        params = context(services=[{"name": "api", "image": "nginx:1.27", "port": 8080},
                                   {"name": "web", "image": "nginx:1.27", "port": 8080}])
        cm, doc = dashboard(render(params))
        self.assertEqual(doc["uid"], "app-dev-alpha")
        self.assertEqual(doc["title"], "App · alpha")
        self.assertEqual(doc["refresh"], "30s")
        self.assertEqual(doc["time"], {"from": "now-1h", "to": "now"})
        self.assertEqual(cm["metadata"]["namespace"], "alpha")
        self.assertEqual(cm["metadata"]["annotations"]["grafana_folder"], "/tmp/dashboards/DigiOrg Apps")
        ns = manifests_of_kind(render(params), "Namespace")[0]
        self.assertEqual(ns["metadata"]["labels"]["platform.digiorg.io/app-id"], "alpha")
        self.assertEqual(doc, dashboard(render(params))[1])
        self.assertLess(len(json.dumps(doc).encode()), 65536)
        self.assertIn("api", doc["description"])
        self.assertIn("web", doc["description"])
        self.assertNotIn("namespace", [v["name"] for v in doc["templating"]["list"]])

    def test_identity_fails_closed_and_does_not_gate_workloads(self):
        params = context()
        self.assertEqual(dashboard(render(context("a" * 32)))[1]["uid"], "app-dev-" + "a" * 32)
        for app in ("a" * 33, "bad\" OR *", "UPPER", "-bad", "bad-"):
            bad = context(app)
            self.assertFalse(any(m.get("metadata", {}).get("labels", {}).get("grafana_dashboard")
                                 for m in manifests_of_kind(render(bad), "ConfigMap")))
        for mode in ("missing-uid", "duplicate", "unobserved", "no-namespace", "terminating"):
            bad = copy.deepcopy(params)
            if mode == "missing-uid":
                bad["oxr"]["metadata"].pop("uid")
            elif mode == "duplicate":
                other = copy.deepcopy(bad["oxr"])
                other["metadata"]["uid"] = "foreign-uid"
                bad["requiredResources"]["dashboardApplications"].append({"Resource": other})
            elif mode == "unobserved":
                bad["requiredResources"]["dashboardApplications"] = []
            elif mode == "no-namespace":
                bad["requiredResources"]["targetNamespace"] = []
            else:
                bad["requiredResources"]["targetNamespace"][0]["Resource"]["metadata"]["deletionTimestamp"] = "now"
            items = render(bad)
            self.assertFalse(any(m.get("metadata", {}).get("labels", {}).get("grafana_dashboard")
                                 for m in manifests_of_kind(items, "ConfigMap")), mode)
        items = render(params)
        obj = next(i for i in items if i.get("metadata", {}).get("annotations", {}).get(
            "krm.kcl.dev/composition-resource-name") == "grafana-dashboard")
        self.assertEqual(obj["metadata"]["annotations"]["krm.kcl.dev/ready"], "True")
        self.assertEqual(obj["spec"]["deletionPolicy"], "Delete")
        self.assertEqual(obj["spec"]["managementPolicies"], ["Observe", "Create", "Update", "Delete"])
        request = next(i for i in items if i.get("kind") == "RequiredResources"
                       and "dashboardApplications" in i["requirements"])
        self.assertEqual(request["requirements"]["dashboardApplications"]["matchLabels"], {})
        self.assertEqual(request["requirements"]["targetNamespace"]["name"], "alpha")

    def test_real_panels_are_literal_scoped_and_no_data_is_not_success(self):
        _, doc = dashboard(render(context()))
        titles = {p["title"] for p in doc["panels"]}
        self.assertTrue({"CPU cores", "Memory working set", "CPU requests / limits", "Memory requests / limits",
                         "Deployment replicas", "Restarts (15m)", "Last termination OOM (not event count)",
                         "Pod waiting / Pending / unschedulable", "Pod network RX / TX", "Logs", "Log volume",
                         "Error / warning log counts", "Level coverage", "Service Pod ownership", "Telemetry confidence"} <= titles)
        prom = []
        logs = []
        for p in doc["panels"]:
            self.assertTrue(p.get("targets"), p)
            for target in p.get("targets", []):
                if "expr" in target:
                    prom.append(target["expr"])
                    self.assertIn('namespace="alpha"', target["expr"])
                    self.assertNotIn("or vector(0)", target["expr"])
                if "query" in target:
                    logs.append(target["query"])
                    self.assertTrue(target["query"].startswith('kubernetes.namespace_name:"alpha"'))
        text = json.dumps(doc)
        self.assertNotIn("namespace_name.keyword", text)
        self.assertNotIn(":raw", text)
        self.assertNotIn("var-namespace", text)
        self.assertIn("level.keyword", text)
        self.assertIn("level.keyword:/([eE][rR][rR]", text)
        self.assertIn("level.keyword:/[wW][aA][rR][nN]", text)
        self.assertIn("kube_replicaset_owner", " ".join(prom))
        self.assertIn('owner_is_controller="true"', " ".join(prom))
        self.assertIn('owner_kind="StatefulSet"', " ".join(prom))
        self.assertIn('owner_kind="DaemonSet"', " ".join(prom))
        self.assertIn("max by(namespace,pod)", " ".join(prom))
        log_panel = next(p for p in doc["panels"] if p["title"] == "Logs")
        self.assertEqual(log_panel["targets"][0]["metrics"][0]["settings"]["limit"], "200")
        variables = {v["name"]: v for v in doc["templating"]["list"]}
        self.assertEqual(variables["logs"]["regex"], r"/^OpenSearch \(Logs\)$/")
        self.assertEqual(variables["logs"]["query"], "elasticsearch")
        for name in ("pod", "container"):
            self.assertIn('namespace="alpha"', variables[name]["query"])
        self.assertIn("No logs observed", log_panel["fieldConfig"]["defaults"]["noValue"])
        self.assertIn("Unknown", next(p for p in doc["panels"] if p["title"] == "Telemetry confidence")["fieldConfig"]["defaults"]["noValue"])
        _, other = dashboard(render(context("bravo")))
        self.assertNotIn('namespace=\\"alpha\\"', json.dumps(other))


if __name__ == "__main__":
    unittest.main()
