"""Issue 372: exercise actual KCL outputs and the shipped enrollment runner."""
import unittest
import contextlib
import http.server
import io
import json
from typing import Any
from pathlib import Path
import runpy
import ssl
import subprocess
import tempfile
import threading
import urllib.parse
import yaml
from render_harness import make_oxr, render, ready_cicd_context, manifests_of_kind


def params(app="sq-app", **kwargs):
    p = {"oxr": make_oxr(appName=app, gitea={"enabled": True, "cicd": False}, **kwargs)}
    p["oxr"]["metadata"]["uid"] = "11111111-1111-4111-8111-111111111111"
    p.update(ready_cicd_context(app))
    p["requiredResources"]["sonarqubeApplications"] = [{"Resource": p["oxr"]}]
    return p


def sonar_jobs(items):
    return [j for j in manifests_of_kind(items, "Job") if j["metadata"]["name"].endswith("-sonarqube-v1")]


class EnrollmentRenderTests(unittest.TestCase):
    def test_duplicate_or_unavailable_app_inventory_blocks_enrollment(self):
        p = params()
        other = json.loads(json.dumps(p["oxr"]))
        other["metadata"]["uid"] = "22222222-2222-4222-8222-222222222222"
        p["requiredResources"]["sonarqubeApplications"].append({"Resource": other})
        self.assertEqual(sonar_jobs(render(p)), [])
        p["requiredResources"].pop("sonarqubeApplications")
        self.assertEqual(sonar_jobs(render(p)), [])

    def test_pending_enrollment_cannot_mark_repo_ready(self):
        items = render(params())
        repo = next(i for i in items if i.get("metadata", {}).get("annotations", {}).get("krm.kcl.dev/composition-resource-name") == "gitea-repo")
        self.assertEqual(repo["metadata"]["annotations"]["krm.kcl.dev/ready"], "False")
        conditions = [c for i in items if i.get("kind") == "Conditions" for c in i["conditions"]]
        condition = next(c["condition"] for c in conditions if c["condition"]["type"] == "SonarQubeEnrollmentReady")
        self.assertEqual(condition["status"], "False")

    def test_disabled_wrong_repo_and_malformed_repo_never_enroll(self):
        for body, status in [(None, 200), ("{}", 200), ("not-json", 200), (json.dumps({"full_name": "DigiOrg/other"}), 200), (json.dumps({"full_name": "DigiOrg/sq-app"}), 403)]:
            p = params()
            response = p["ocds"]["gitea-repo"]["Resource"]["status"]["response"]
            response.update(body=body, statusCode=status)
            self.assertEqual(sonar_jobs(render(p)), [])
        p = params()
        p["oxr"]["spec"]["gitea"]["enabled"] = False
        self.assertEqual(sonar_jobs(render(p)), [])

    def test_completed_job_is_stable_historical_enrollment_not_generation_loop(self):
        p = params()
        first = sonar_jobs(render(p))[0]
        p["ocds"]["sonarqube-enrollment"] = {"Resource": {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}}
        p["oxr"]["metadata"]["generation"] = 99
        items = render(p)
        self.assertEqual(sonar_jobs(items)[0], first)
        repo = next(i for i in items if i.get("metadata", {}).get("annotations", {}).get("krm.kcl.dev/composition-resource-name") == "gitea-repo")
        self.assertNotIn("krm.kcl.dev/ready", repo["metadata"]["annotations"])
        conditions = [c["condition"] for i in items if i.get("kind") == "Conditions" for c in i["conditions"]]
        self.assertEqual(next(c["status"] for c in conditions if c["type"] == "SonarQubeEnrollmentReady"), "True")
        # Loss of repo observation must not remove/recreate the completed Job.
        p["ocds"].pop("gitea-repo")
        self.assertEqual(sonar_jobs(render(p))[0], first)

    def test_repo_ready_without_cicd_renders_one_bounded_job(self):
        jobs = sonar_jobs(render(params()))
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertNotIn("ttlSecondsAfterFinished", job["spec"])
        self.assertLessEqual(job["spec"]["activeDeadlineSeconds"], 300)
        pod = job["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertIn("@sha256:", pod["containers"][0]["image"])


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.cert, cls.key = cls.root / "ca.crt", cls.root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                        "-keyout", str(cls.key), "-out", str(cls.cert)], check=True, capture_output=True)
        rendered = subprocess.run(["kustomize", "build", "compositions/local"], check=True, capture_output=True, text=True)
        maps = [d for d in yaml.safe_load_all(rendered.stdout) if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "sonarqube-onboarding-runner-v1"]
        if len(maps) != 1:
            raise AssertionError("shipped enrollment runner ConfigMap is missing")
        script = cls.root / "enroll.py"
        script.write_text(maps[0]["data"]["enroll.py"])
        cls.runner = runpy.run_path(str(script))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.calls = []
        self.project = None
        self.token = "synthetic-enrollment-credential"
        self.token_file = self.root / "token"
        self.token_file.write_text(self.token)
        self.approvals = self.root / "approvals.json"
        self.approvals.write_text("[]")
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                self.handle_api()

            def do_POST(self):
                self.handle_api()

            def handle_api(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                fixture.calls.append((self.command, self.path, body))
                if self.headers.get("Authorization") != "Bearer " + fixture.token:
                    status, payload = 401, {"errors": [{"msg": "unauthorized"}]}
                else:
                    status, payload = fixture.respond(self.command, self.path, body)
                if status == 0:
                    self.close_connection = True
                    return
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if status in (301, 302, 307, 308):
                    self.send_header("Location", fixture.base + "/credential-sink")
                self.end_headers()
                self.wfile.write(payload if isinstance(payload, bytes) else json.dumps(payload).encode())

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.cert), str(self.key))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "https://localhost:%s/sonarqube" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def respond(self, method, path, body, /) -> tuple[int, Any]:
        if path.endswith("/api/system/status"):
            return 200, {"status": "UP"}
        if path.endswith("/api/users/current"):
            return 200, {"login": "digiorg-provisioner"}
        if path.endswith("/api/authentication/validate"):
            return 200, {"valid": True}
        if path.endswith("/api/qualitygates/list"):
            return 200, {"qualitygates": [{"name": "Sonar way", "isDefault": True, "isBuiltIn": True}]}
        if "/api/qualitygates/get_by_project?" in path:
            return 200, {"qualityGate": {"name": "Sonar way", "default": True}}
        if "/api/qualityprofiles/search" in path:
            return 200, {"profiles": [{"name": "Sonar way", "isDefault": True, "isBuiltIn": True, "language": "py"}]}
        if "/api/components/show?" in path:
            if self.project is None:
                return 404, {"errors": [{"msg": "Component key 'digiorg:gitea:DigiOrg:sq-app' not found"}]}
            return 200, {"component": self.project, "ancestors": []}
        if path.endswith("/api/projects/create") and method == "POST":
            data = urllib.parse.parse_qs(body.decode())
            self.assertEqual(data, {"project": ["digiorg:gitea:DigiOrg:sq-app"], "name": ["sq-app"], "visibility": ["private"], "mainBranch": ["main"]})
            self.project = {"key": data["project"][0], "name": "sq-app", "qualifier": "TRK", "visibility": "private"}
            return 200, {"project": self.project}
        return 500, {}

    def run_enroll(self, **kwargs):
        args = dict(base=self.base, token_file=self.token_file, ca_file=self.cert,
                    app="sq-app", uid="11111111-1111-4111-8111-111111111111", approvals_file=self.approvals)
        args.update(kwargs)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            try:
                result = self.runner["enroll"](**args)
            finally:
                self.assertNotIn(self.token, output.getvalue())
        return result

    def test_rotation_reads_new_secret_and_revoked_token_cannot_write(self):
        self.token_file.write_text("fixture-revoked-token")
        with self.assertRaises(self.runner["EnrollmentError"]):
            self.run_enroll()
        self.assertFalse(any(m == "POST" for m, _, _ in self.calls))
        self.token = "fixture-rotated-token"
        self.token_file.write_text(self.token)
        self.run_enroll()
        self.assertEqual(sum(m == "POST" for m, _, _ in self.calls), 1)

    def test_readback_rejects_wrong_key_public_and_nonproject_objects(self):
        original = self.respond
        for field, value in [("key", "other"), ("visibility", "public"), ("qualifier", "DIR")]:
            with self.subTest(field=field):
                self.calls.clear()
                self.project = None
                def respond(m, p, b):
                    status, body = original(m, p, b)
                    if "/api/components/show?" in p and status == 200:
                        body["component"][field] = value
                    return status, body
                self.respond = respond
                with self.assertRaisesRegex(self.runner["EnrollmentError"], "IdentityMismatch"):
                    self.run_enroll()
                self.assertEqual(sum(m == "POST" for m, _, _ in self.calls), 1)

    def test_admin_credential_is_never_a_provisioner_fallback(self):
        original = self.respond
        self.respond = lambda m, p, b: (200, {"login": "admin"}) if p.endswith("/api/users/current") else original(m, p, b)
        with self.assertRaisesRegex(self.runner["EnrollmentError"], "ProvisionerIdentityMismatch"):
            self.run_enroll()
        self.assertFalse(any(m == "POST" for m, _, _ in self.calls))

    def test_pinned_non_root_image_executes_shipped_runner_against_tls_fixture(self):
        items = render(params())
        job = sonar_jobs(items)[0]
        image = job["spec"]["template"]["spec"]["containers"][0]["image"]
        source = self.root / "enroll.py"
        entry = self.root / "image-entry.py"
        entry.write_text("import runpy\nrunpy.run_path('/runner/enroll.py')['enroll'](base=" + repr(self.base) + ", token_file='/var/run/sonarqube/token', ca_file='/var/run/digiorg-ca/ca.crt', app='sq-app', uid='11111111-1111-4111-8111-111111111111', approvals_file='/absent')\n")
        command = ["docker", "run", "--rm", "--network", "host", "--read-only", "--user", "65534:65534", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", "64m", "--cpus", "0.1"]
        for local, target in [(source, "/runner/enroll.py"), (entry, "/image-entry.py"), (self.token_file, "/var/run/sonarqube/token"), (self.cert, "/var/run/digiorg-ca/ca.crt")]:
            command += ["--mount", "type=bind,src=%s,dst=%s,readonly" % (local, target)]
        result = subprocess.run(command + [image, "python3", "-B", "/image-entry.py"], capture_output=True, text=True, timeout=120)
        self.assertNotIn(self.token, result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([m for m, _, _ in self.calls].count("POST"), 1)

    def test_adoption_does_not_accept_custom_project_gate(self):
        self.project = {"key": "digiorg:gitea:DigiOrg:sq-app", "qualifier": "TRK", "visibility": "private"}
        self.approvals.write_text(json.dumps([{"key": self.project["key"], "xrUID": "11111111-1111-4111-8111-111111111111", "approvedBy": "Chris", "approvalRef": "https://github.com/digiorg/core/issues/372#issuecomment-123"}]))
        original = self.respond
        self.respond = lambda m, p, b: (200, {"qualityGate": {"name": "custom", "default": False}}) if "/api/qualitygates/get_by_project?" in p else original(m, p, b)
        with self.assertRaisesRegex(self.runner["EnrollmentError"], "ProjectPolicyMismatch"):
            self.run_enroll()
        self.assertFalse(any(m == "POST" for m, _, _ in self.calls))

    def test_redirect_is_never_followed_with_credentials(self):
        self.respond = lambda m, p, b: (302, {})
        with self.assertRaises(self.runner["EnrollmentError"]):
            self.run_enroll()
        self.assertEqual(len(self.calls), 1)

    def test_invalid_identity_has_no_network_capability(self):
        for values in ({"app": "../../other"}, {"uid": ""}, {"base": "http://localhost/sonarqube"}):
            with self.subTest(values=values):
                with self.assertRaisesRegex(self.runner["EnrollmentError"], "InvalidInput"):
                    self.run_enroll(**values)
                self.assertEqual(self.calls, [])

    def test_bad_certificate_and_hostname_fail_before_http(self):
        with self.assertRaises(self.runner["EnrollmentError"]):
            self.run_enroll(base=self.base.replace("localhost", "127.0.0.1"))
        self.assertEqual(self.calls, [])

    def test_malformed_response_is_terminal_without_write(self):
        self.respond = lambda m, p, b: (200, b"not-json-" + self.token.encode())
        with self.assertRaises(self.runner["EnrollmentError"]):
            self.run_enroll()
        self.assertEqual(len(self.calls), 1)

    def test_get_retry_budget_is_bounded(self):
        original = self.respond
        remaining = [2]
        def respond(m, p, b):
            if p.endswith("/api/system/status") and remaining[0] > 0:
                remaining[0] -= 1
                return 503, {}
            return original(m, p, b)
        self.respond = respond
        self.run_enroll()
        self.assertEqual(len([p for _, p, _ in self.calls if p.endswith("/api/system/status")]), 3)
        self.calls.clear()
        self.project = None
        remaining[0] = 20
        with self.assertRaises(self.runner["EnrollmentError"]):
            self.run_enroll()
        self.assertEqual(len(self.calls), 3)
        self.assertFalse(any(m == "POST" for m, _, _ in self.calls))

    def test_ambiguous_creation_readback_never_retries_post_or_adopts(self):
        original = self.respond
        for status, payload in [(503, {}), (400, {}), (409, {}), (0, {}), (200, b"broken-json")]:
            with self.subTest(status=status):
                self.calls.clear()
                self.project = None
                def respond(m, p, b):
                    result = original(m, p, b)
                    return (status, payload) if m == "POST" else result
                self.respond = respond
                with self.assertRaisesRegex(self.runner["EnrollmentError"], "AdoptionRequired"):
                    self.run_enroll()
                self.assertEqual([m for m, _, _ in self.calls].count("POST"), 1)
                self.assertIn("/api/components/show?", self.calls[-1][1])
                with self.assertRaisesRegex(self.runner["EnrollmentError"], "AdoptionRequired"):
                    self.run_enroll()
                self.assertEqual([m for m, _, _ in self.calls].count("POST"), 1)

    def test_unproven_prerequisites_never_create(self):
        original = self.respond
        for route, status, body in [
            ("/api/system/status", 200, {"status": "DB_MIGRATION_RUNNING"}),
            ("/api/authentication/validate", 200, {"valid": False}),
            ("/api/qualitygates/list", 200, {"qualitygates": [{"name": "custom", "isDefault": True}]}),
            ("/api/qualityprofiles/search", 200, {"profiles": []}),
            ("/api/components/show?", 200, {"component": {}}),
            ("/api/components/show?", 401, {"errors": [{"msg": self.token}]}),
            ("/api/components/show?", 403, {}),
            ("/api/components/show?", 404, {"errors": [{"msg": "proxy missing"}]}),
            ("/api/components/show?", 500, {}),
        ]:
            with self.subTest(route=route, status=status, body=body):
                self.calls.clear()
                self.project = None
                self.respond = lambda m, p, b: (status, body) if route in p else original(m, p, b)
                with self.assertRaises(self.runner["EnrollmentError"]):
                    self.run_enroll()
                self.assertFalse(any(m == "POST" for m, _, _ in self.calls))
        self.respond = original

    def test_existing_project_requires_exact_uid_approval_without_writes(self):
        self.project = {"key": "digiorg:gitea:DigiOrg:sq-app", "name": "sq-app", "qualifier": "TRK", "visibility": "private"}
        with self.assertRaisesRegex(self.runner["EnrollmentError"], "AdoptionRequired"):
            self.run_enroll()
        self.assertFalse(any(m == "POST" for m, _, _ in self.calls))
        approval = {"key": self.project["key"], "xrUID": "wrong-owner", "approvedBy": "Chris", "approvalRef": "https://github.com/digiorg/core/issues/372#issuecomment-123"}
        self.approvals.write_text(json.dumps([approval]))
        with self.assertRaisesRegex(self.runner["EnrollmentError"], "AdoptionRequired"):
            self.run_enroll()
        approval["xrUID"] = "11111111-1111-4111-8111-111111111111"
        self.approvals.write_text(json.dumps([approval]))
        self.run_enroll()
        self.assertFalse(any(m == "POST" for m, _, _ in self.calls))

    def test_missing_project_creates_once_and_reads_back_private_identity(self):
        self.run_enroll()
        self.assertEqual([m for m, _, _ in self.calls].count("POST"), 1)
        self.assertIn("/api/components/show?", self.calls[-1][1])
        assert self.project is not None
        self.assertEqual(self.project["visibility"], "private")


if __name__ == "__main__":
    unittest.main()
