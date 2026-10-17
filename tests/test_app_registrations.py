import copy
import importlib.util
from pathlib import Path
import unittest
import json
import subprocess
import tempfile
import yaml
from render_harness import render, _find_kcl_cli, _subprocess_env

ROOT = Path(__file__).resolve().parents[1]


def registration():
    return {'contractVersion': 1, 'appId': 'alpha', 'namespace': 'alpha', 'ownerTeam': 'team-a', 'dedicatedNamespace': True,
            'dashboardDesired': True, 'expectedWorkloads': [{'kind': 'Deployment', 'name': 'web'}]}


def snapshot():
    return {'appClaims': [], 'namespaces': [{'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': 'alpha'}, 'status': {'phase': 'Active'}}],
            'configMaps': [], 'grafanaDashboards': []}


def adapter():
    path = ROOT / 'scripts/render_app_registrations.py'
    if not path.exists():
        raise AssertionError('canonical registration adapter missing')
    spec = importlib.util.spec_from_file_location('registration_adapter', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RegistrationTests(unittest.TestCase):
    def test_invalid_or_colliding_registration_fails_before_render(self):
        mod = adapter()
        variants = []
        for field, value in [('appId', 'bad\" OR *'), ('appId', 'a' * 33), ('namespace', 'shared'),
                             ('namespace', 'kube-system'), ('dedicatedNamespace', False), ('ownerTeam', 'x\"'),
                             ('contractVersion', 2), ('dashboardDesired', 'true')]:
            entry = registration()
            entry[field] = value
            variants.append(([entry], snapshot()))
        for workload in ([{'kind': 'Job', 'name': 'web'}], [{'kind': 'Deployment', 'name': 'web'}] * 2):
            entry = registration()
            entry['expectedWorkloads'] = workload
            variants.append(([entry], snapshot()))
        variants.append(([registration(), registration()], snapshot()))
        for field, value in [('appClaims', [{'spec': {'appName': 'alpha'}}]),
                             ('configMaps', [{'metadata': {'name': 'grafana-app-dev-alpha', 'namespace': 'alpha'}}]),
                             ('grafanaDashboards', [{'uid': 'app-dev-alpha', 'owner': 'manual'}])]:
            observed = snapshot()
            observed[field] = value
            variants.append(([registration()], observed))
        observed = snapshot()
        observed.pop('appClaims')
        variants.append(([registration()], observed))
        for entries, observed in variants:
            with self.subTest(entries=entries, observed=observed):
                called = []
                with self.assertRaises(ValueError):
                    mod.generate(entries, observed, lambda p: called.append(p))
                self.assertFalse(called, 'validation must precede render/output')

    def test_cli_executes_canonical_kcl_and_emits_only_derived_resources(self):
        import sys
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            (directory / 'registrations.yaml').write_text(yaml.safe_dump({'apps': [registration()]}))
            (directory / 'snapshot.json').write_text(json.dumps(snapshot()))
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/render_app_registrations.py'), '--registrations', str(directory / 'registrations.yaml'),
                                     '--snapshot', str(directory / 'snapshot.json'), '--kcl', _find_kcl_cli()], capture_output=True, text=True, env=_subprocess_env(), timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(yaml.safe_load_all(result.stdout)), adapter().generate([registration()], snapshot(), render))

    def test_registered_app_uses_canonical_kcl_without_managing_workloads(self):
        mod = adapter()
        docs = mod.generate([registration()], snapshot(), render)
        self.assertEqual([d['kind'] for d in docs], ['ConfigMap', 'PrometheusRule'])
        cm = docs[0]
        self.assertEqual(cm['metadata']['annotations']['platform.digiorg.io/dashboard-owner'], 'gitops:alpha')
        self.assertEqual(cm['metadata']['name'], 'grafana-app-dev-alpha')
        self.assertEqual(docs, mod.generate([registration()], snapshot(), render))
        rules = docs[1]['spec']['groups'][0]['rules']
        self.assertEqual([r['record'] for r in rules], ['digiorg_registered_app_info', 'digiorg_registered_app_service_info'])
        self.assertEqual(rules[1]['labels']['service'], 'web')
        self.assertEqual(rules[0]['labels']['namespace'], 'alpha')
        self.assertEqual(mod.generate([], snapshot(), render), [])


if __name__ == '__main__':
    unittest.main()
