#!/usr/bin/env python3
"""Issue #301: safe, atomic fresh-source scaffolding contracts and behavior."""
import base64
import json
import os

import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

sys.path.insert(0, os.path.dirname(__file__))
from render_harness import by_kind, make_oxr, ready_cicd_context, render  # noqa: E402

MARKER = "v1"
NGINX = "nginx:1.30-alpine@sha256:ec664813a30459a8e7176315268a623f6b31abc370eeac51c7de81cd4ec4d451"
CURL_IMAGE = "curlimages/curl:8.16.0@sha256:463eaf6072688fe96ac64fa623fe73e1dbe25d8ad6c34404a669ad3ce1f104b6"
TEST_TOKEN = 'tok en;$`"\\sentinel'


def _condition(status="True", cond_type="Ready"):
    return {"Resource": {"status": {"conditions": [{"type": cond_type, "status": status}]}}}


def _repo_response(status=200, body=None):
    if body is None:
        body = {"full_name": "DigiOrg/myapp", "name": "myapp", "owner": {"login": "DigiOrg"}}
    return {"Resource": {"status": {"response": {"statusCode": status, "body": json.dumps(body)}}}}


def _render(services=None, observer=None, credentials_ready=True, gitea=None,
            repo_observation="ready", generation=1, extra_ocds=None):
    app = "myapp"
    services = services if services is not None else [
        {"name": "web", "image": "unused", "port": 8080,
         "build": {"enabled": True, "context": "."}}
    ]
    ready = ready_cicd_context(app)
    ready["ocds"].pop("source-scaffold-0", None)
    # Tests in this module control the generation-specific observer explicitly.
    ready["ocds"].pop(f"ss-o-{MARKER}-g1", None)
    ready["ocds"].pop(f"ss-c-{MARKER}-g1", None)
    ready["ocds"][f"ss-c-{MARKER}-g{generation}"] = _condition("True")
    if repo_observation == "ready":
        ready["ocds"]["gitea-repo"] = _repo_response()
    elif repo_observation is None:
        ready["ocds"].pop("gitea-repo", None)
    else:
        ready["ocds"]["gitea-repo"] = repo_observation
    if observer is not None:
        ready["ocds"][f"ss-o-{MARKER}-g{generation}"] = observer
    ready["ocds"].update(extra_ocds or {})
    return render({
        "oxr": make_oxr(
            appName=app,
            gitea=gitea or {"enabled": True, "visibility": "private", "cicd": True},
            services=services,
            generation=generation,
        ),
        "ocds": ready["ocds"] if credentials_ready else {},
        "requiredResources": ready["requiredResources"] if credentials_ready else {},
    })


def _slug(item):
    return item.get("metadata", {}).get("annotations", {}).get(
        "krm.kcl.dev/composition-resource-name"
    )


def _objects(items):
    return {_slug(x): x for x in by_kind(items, "Object")}


def _requests(items):
    return {_slug(x): x for x in by_kind(items, "Request")}


def _manifest(obj):
    return obj["spec"]["forProvider"]["manifest"]


def _job_and_script(items):
    objects = _objects(items)
    job_obj = next(v for k, v in objects.items() if k.startswith(f"ss-j-{MARKER}-g"))
    job = _manifest(job_obj)
    container = job["spec"]["template"]["spec"]["containers"][0]
    return job_obj, job, container, container["args"][0]


class RenderContractTest(unittest.TestCase):
    def test_generation_is_part_of_job_resource_and_observer_identity(self):
        first = _objects(_render(generation=1))
        same = _objects(_render(generation=1))
        second = _objects(_render(generation=2))

        for prefix in ("c", "j", "o"):
            slug1 = f"ss-{prefix}-{MARKER}-g1"
            slug2 = f"ss-{prefix}-{MARKER}-g2"
            self.assertIn(slug1, first)
            self.assertIn(slug1, same)
            self.assertIn(slug2, second)
            self.assertNotIn(slug1, second)
        self.assertEqual(
            _manifest(first[f"ss-j-{MARKER}-g1"])["metadata"]["name"],
            "myapp-ss-v1-g1",
        )
        self.assertEqual(
            _manifest(second[f"ss-o-{MARKER}-g2"])["metadata"]["name"],
            "myapp-ss-v1-g2",
        )

    def test_max_app_name_and_int64_generation_fit_kubernetes_dns_names(self):
        app = "a" * 32
        generation = 9223372036854775807
        ready = ready_cicd_context(app)
        ready["ocds"]["gitea-repo"] = _repo_response(
            body={"full_name": f"DigiOrg/{app}"}
        )
        ready["ocds"][f"ss-c-{MARKER}-g{generation}"] = _condition("True")
        items = render({
            "oxr": make_oxr(
                appName=app, generation=generation,
                gitea={"enabled": True, "visibility": "private", "cicd": True},
                services=[{"name": "web", "image": "x", "port": 8080,
                           "build": {"enabled": True, "context": "."}}],
            ),
            **ready,
        })
        scaffold_objects = [
            item for item in by_kind(items, "Object")
            if "-v1-g" in (_slug(item) or "")
        ]
        self.assertEqual(len(scaffold_objects), 3)
        for item in scaffold_objects:
            for name in (item["metadata"]["name"], _manifest(item)["metadata"]["name"]):
                self.assertLessEqual(len(name), 63, name)
                self.assertRegex(name, r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

    def test_realistic_fresh_xr_generation_seven_uses_generation_seven_identity(self):
        oxr = make_oxr(
            appName="myapp", generation=7,
            gitea={"enabled": True, "visibility": "private", "cicd": True},
            services=[{"name": "web", "image": "x", "port": 8080,
                       "build": {"enabled": True, "context": "."}}],
        )
        oxr["status"]["conditions"] = [{
            "type": "Synced", "status": "True", "observedGeneration": 7,
        }]
        self.assertEqual(oxr["metadata"]["generation"], 7)
        self.assertEqual(oxr["status"]["conditions"][0]["observedGeneration"], 7)
        ready = ready_cicd_context("myapp")
        ready["ocds"][f"ss-c-{MARKER}-g7"] = _condition("True")
        # Render a semantically equivalent fresh OXR; mutating option("params") data
        # makes this KCL CLI version echo params as an additional top-level document.
        oxr["status"] = {}
        items = render({"oxr": oxr, **ready})
        self.assertIn(f"ss-j-{MARKER}-g7", _objects(items))

    def test_new_generation_waits_for_its_own_observer(self):
        old_ready = {f"ss-o-{MARKER}-g1": _condition("True")}
        self.assertNotIn("gitea-cicd", _requests(_render(generation=2, extra_ocds=old_ready)))
        self.assertIn("gitea-cicd", _requests(_render(generation=2, observer=_condition("True"))))

    def test_missing_or_malformed_generation_fails_closed(self):
        for generation in (None, "2", 0, -1, 1.5):
            with self.subTest(generation=generation):
                oxr = make_oxr(
                    appName="myapp", generation=generation,
                    gitea={"enabled": True, "visibility": "private", "cicd": True},
                    services=[{"name": "web", "image": "x", "port": 8080,
                               "build": {"enabled": True, "context": "."}}],
                )
                if generation is None:
                    oxr["metadata"].pop("generation")
                ready = ready_cicd_context("myapp")
                items = render({"oxr": oxr, **ready})
                slugs = {_slug(item) for item in items}
                self.assertFalse(any((slug or "").startswith("ss-") for slug in slugs))
                self.assertNotIn("gitea-cicd", slugs)

    def test_repo_request_always_renders_but_scaffold_requires_exact_repo_identity(self):
        unsafe = {
            "no ocd": None,
            "ready condition without response": _condition(),
            "malformed json": {"Resource": {"status": {"response": {"statusCode": 200, "body": "{"}}}},
            "404": _repo_response(404, {"message": "not found"}),
            "wrong full_name": _repo_response(200, {"full_name": "OtherOrg/myapp"}),
            "401": _repo_response(401, {"message": "unauthorized"}),
            "500": _repo_response(500, {"message": "error"}),
        }
        for label, observation in unsafe.items():
            with self.subTest(label=label):
                items = _render(repo_observation=observation)
                self.assertIn("gitea-repo", _requests(items))
                objects = _objects(items)
                self.assertNotIn(f"ss-j-{MARKER}-g1", objects)
                self.assertNotIn(f"ss-o-{MARKER}-g1", objects)
                self.assertNotIn("gitea-cicd", _requests(items))

        ready_items = _render(repo_observation=_repo_response())
        self.assertIn(f"ss-c-{MARKER}-g1", _objects(ready_items))

    def test_config_object_readiness_sequences_job_and_observer_across_reconciles(self):
        ready = ready_cicd_context("myapp")
        ready["ocds"].pop(f"ss-c-{MARKER}-g1", None)
        initial = _render(extra_ocds={f"ss-c-{MARKER}-g1": {}})
        initial_objects = _objects(initial)
        self.assertIn(f"ss-c-{MARKER}-g1", initial_objects)
        self.assertNotIn(f"ss-j-{MARKER}-g1", initial_objects)
        self.assertNotIn(f"ss-o-{MARKER}-g1", initial_objects)
        self.assertNotIn("gitea-cicd", _requests(initial))

        config_ready = _render(extra_ocds={f"ss-c-{MARKER}-g1": _condition("True")})
        config_objects = _objects(config_ready)
        self.assertIn(f"ss-j-{MARKER}-g1", config_objects)
        self.assertIn(f"ss-o-{MARKER}-g1", config_objects)
        self.assertNotIn("gitea-cicd", _requests(config_ready))

    def test_scaffold_uses_no_provider_http_request(self):
        items = _render()
        scaffold_http = [x for x in by_kind(items, "Request") if "source-scaffold" in (_slug(x) or "")]
        self.assertEqual(scaffold_http, [])

    def test_job_and_observer_have_explicit_v1_marker_and_exact_identity(self):
        objects = _objects(_render())
        self.assertIn(f"ss-j-{MARKER}-g1", objects)
        self.assertIn(f"ss-o-{MARKER}-g1", objects)
        job_obj = objects[f"ss-j-{MARKER}-g1"]
        observer = objects[f"ss-o-{MARKER}-g1"]
        job = _manifest(job_obj)
        observed = _manifest(observer)
        self.assertIn(MARKER, job["metadata"]["name"])
        self.assertEqual(observed["metadata"], job["metadata"])
        # Marker changes are mandatory whenever the embedded script/template changes.
        self.assertEqual(job_obj["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"], f"ss-j-{MARKER}-g1")

    def test_job_security_and_secret_file_mount_contract(self):
        obj, job, container, script = _job_and_script(_render())
        spec = job["spec"]
        pod = spec["template"]["spec"]
        self.assertEqual(job["metadata"]["namespace"], "crossplane-system")
        self.assertEqual(spec["backoffLimit"], 0)
        self.assertGreater(spec["activeDeadlineSeconds"], 0)
        self.assertLessEqual(spec["activeDeadlineSeconds"], 300)
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["securityContext"], {
            "runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534,
            "fsGroup": 65534, "seccompProfile": {"type": "RuntimeDefault"},
        })
        self.assertEqual(container["image"], CURL_IMAGE)
        self.assertEqual(container["securityContext"], {
            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        })
        self.assertTrue(any(v.get("emptyDir") == {} for v in pod["volumes"]))
        secret_volumes = [v["secret"] for v in pod["volumes"] if "secret" in v]
        self.assertEqual(
            {(v["secretName"], tuple((i["key"], i["path"]) for i in v["items"])) for v in secret_volumes},
            {("crossplane-gitea-credentials", (("token", "token"),)),
             ("digiorg-local-ca", (("ca.crt", "ca.crt"),))},
        )
        self.assertTrue(all(m.get("readOnly") for m in container["volumeMounts"] if m["name"] != "workspace"))
        rendered = json.dumps(job)
        self.assertNotIn("secretKeyRef", rendered)
        self.assertNotIn("valueFrom", rendered)
        self.assertNotIn("-k", script)
        self.assertNotIn("--insecure", script)
        self.assertIn("--cacert", script)
        self.assertNotIn("set -x", script)
        self.assertIn("--config", script)

    def test_one_deterministic_batch_and_dockerfile_only_payload(self):
        services = [
            {"name": "web", "image": "x", "port": 9090, "build": {"enabled": True, "context": "."}},
            {"name": "api", "image": "x", "port": 8080, "build": {"enabled": True, "context": "services/api"}},
        ]
        objects = _objects(_render(services))
        config = _manifest(objects[f"ss-c-{MARKER}-g1"])
        lines = [json.loads(line) for line in config["data"]["files.jsonl"].splitlines()]
        self.assertEqual([x["path"] for x in lines], ["Dockerfile", "services/api/Dockerfile"])
        for record, port in zip(lines, (9090, 8080)):
            self.assertEqual(record["operation"], "create")
            dockerfile = base64.b64decode(record["content"]).decode()
            self.assertEqual(dockerfile.count("FROM "), 1)
            self.assertIn(f"FROM {NGINX}", dockerfile)
            self.assertIn(f"listen {port};", dockerfile)
            self.assertIn(f"EXPOSE {port}", dockerfile)
        _obj, _job, _container, script = _job_and_script(_render(services))
        self.assertIn("/repos/DigiOrg/$APP_NAME/contents", script)
        self.assertNotIn("PUT", script)
        self.assertNotIn("upload", script)
        self.assertNotIn("update", script.lower())

    def test_duplicate_context_fails_closed(self):
        services = [
            {"name": "a", "image": "x", "port": 8080, "build": {"enabled": True, "context": "."}},
            {"name": "b", "image": "x", "port": 9090, "build": {"enabled": True, "context": "."}},
        ]
        items = _render(services, observer=_condition())
        slugs = {_slug(x) for x in items}
        self.assertFalse(any((s or "").startswith("ss-") for s in slugs))
        self.assertNotIn("gitea-cicd", slugs)

    def test_observer_is_observe_only_and_complete_true_is_the_only_ready_state(self):
        observer = _objects(_render())[f"ss-o-{MARKER}-g1"]
        self.assertEqual(observer["spec"]["managementPolicies"], ["Observe"])
        readiness = observer["spec"]["readiness"]
        self.assertEqual(readiness["policy"], "DeriveFromCelQuery")
        cel = readiness["celQuery"]
        self.assertIn('type == "Complete"', cel)
        self.assertIn('status == "True"', cel)
        self.assertIn('type == "Failed"', cel)

    def test_no_buildable_services_need_no_scaffold_job(self):
        items = _render(services=[], observer=None)
        slugs = {_slug(item) for item in items}
        self.assertFalse(any((slug or "").startswith("ss-") for slug in slugs))
        self.assertIn("gitea-cicd", _requests(items))

    def test_workflow_gate_requires_observer_ready_true(self):
        for observation in (None, {}, _condition("False"), _condition("Unknown"), _condition("True", "Synced")):
            with self.subTest(observation=observation):
                self.assertNotIn("gitea-cicd", _requests(_render(observer=observation)))
        self.assertIn("gitea-cicd", _requests(_render(observer=_condition("True"))))


class FakeGitea:
    def __init__(self, initial=(), get_status=None, post_mode="201", token=TEST_TOKEN):
        self.files = {p: b"owned-by-user" for p in initial}
        self.get_status = get_status
        self.post_mode = post_mode
        self.token = token
        self.posts = []
        self.authorized_requests = 0
        self.unauthorized_requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def authorized(self):
                if self.headers.get("Authorization") == "token " + outer.token:
                    outer.authorized_requests += 1
                    return True
                outer.unauthorized_requests += 1
                self.send_response(401)
                self.end_headers()
                return False

            def do_GET(self):
                if not self.authorized():
                    return
                path = unquote(urlsplit(self.path).path)
                marker = "/contents/"
                file_path = path.split(marker, 1)[1] if marker in path else ""
                if outer.get_status is not None:
                    self.send_response(outer.get_status)
                    self.end_headers()
                    return
                if file_path in outer.files:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{}')
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                if not self.authorized():
                    return
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
                outer.posts.append(payload)
                if outer.post_mode in ("201", "lost"):
                    for f in payload["files"]:
                        outer.files[f["path"]] = base64.b64decode(f["content"])
                elif outer.post_mode == "422-race":
                    for f in payload["files"]:
                        outer.files[f["path"]] = base64.b64decode(f["content"])
                if outer.post_mode == "lost":
                    self.close_connection = True
                    return
                status = 201 if outer.post_mode == "201" else 422 if outer.post_mode == "422-race" else 500
                self.send_response(status)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_port}/api/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class ExecutableBehaviorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        items = _render([
            {"name": "web", "image": "x", "port": 9090, "build": {"enabled": True, "context": "."}},
            {"name": "api", "image": "x", "port": 8080, "build": {"enabled": True, "context": "services/api"}},
        ])
        objects = _objects(items)
        cls.script = _job_and_script(items)[3]
        cls.jsonl = _manifest(objects[f"ss-c-{MARKER}-g1"])["data"]["files.jsonl"]

    def run_script(self, fake, token=TEST_TOKEN):
        with tempfile.TemporaryDirectory() as td:
            token_path = os.path.join(td, "token")
            ca_path = os.path.join(td, "ca.crt")
            files_path = os.path.join(td, "files.jsonl")
            for path, data in ((token_path, token), (ca_path, "unused"), (files_path, self.jsonl)):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(data)
            env = {**os.environ, "API_BASE": fake.base, "APP_NAME": "myapp",
                   "TOKEN_FILE": token_path, "CA_FILE": ca_path,
                   "FILES_FILE": files_path, "WORKSPACE": td,
                   "RETRY_DELAY": "0"}
            proc = subprocess.run(["sh", "-n", "-c", self.script], text=True, capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            proc = subprocess.run(["sh", "-c", self.script], env=env, text=True,
                                  capture_output=True, timeout=20)
            if token:
                self.assertNotIn(token, proc.stdout + proc.stderr)
            return proc

    def with_fake(self, **kwargs):
        return FakeGitea(**kwargs)

    def test_preserves_existing_bytes_and_posts_zero(self):
        fake = self.with_fake(initial=("Dockerfile", "services/api/Dockerfile"))
        before = dict(fake.files)
        try:
            proc = self.run_script(fake)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(fake.posts, [])
            self.assertEqual(fake.files, before)
            self.assertGreater(fake.authorized_requests, 0)
            self.assertEqual(fake.unauthorized_requests, 0)
        finally:
            fake.close()

    def test_mixed_200_404_posts_one_create_only_batch_then_readback(self):
        fake = self.with_fake(initial=("Dockerfile",))
        try:
            proc = self.run_script(fake)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(fake.posts), 1)
            self.assertEqual(fake.posts[0]["branch"], "main")
            self.assertEqual([(f["operation"], f["path"]) for f in fake.posts[0]["files"]],
                             [("create", "services/api/Dockerfile")])
            self.assertEqual(fake.files["Dockerfile"], b"owned-by-user")
        finally:
            fake.close()

    def test_unsafe_get_statuses_fail_closed_without_post(self):
        for status in (401, 403, 429, 500):
            fake = self.with_fake(get_status=status)
            try:
                proc = self.run_script(fake)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(fake.posts, [])
            finally:
                fake.close()

    def test_cr_or_lf_in_token_fails_before_any_http_request(self):
        for token in (
            "",
            "\n",
            "\r",
            TEST_TOKEN + "\n",
            TEST_TOKEN + "\nInjected: header",
            TEST_TOKEN + "\rInjected: header",
        ):
            fake = self.with_fake()
            try:
                proc = self.run_script(fake, token=token)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(fake.authorized_requests, 0)
                self.assertEqual(fake.unauthorized_requests, 0)
                self.assertEqual(fake.posts, [])
            finally:
                fake.close()

    def test_timeout_fails_closed_without_post(self):
        fake = self.with_fake()
        fake.close()
        proc = self.run_script(fake)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(fake.posts, [])

    def test_lost_response_committed_succeeds_without_second_post(self):
        fake = self.with_fake(post_mode="lost")
        try:
            proc = self.run_script(fake)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(fake.posts), 1)
        finally:
            fake.close()

    def test_failed_post_without_commit_fails_without_second_post(self):
        fake = self.with_fake(post_mode="500")
        try:
            proc = self.run_script(fake)
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(len(fake.posts), 1)
        finally:
            fake.close()

    def test_422_race_succeeds_after_readback_and_never_overwrites(self):
        fake = self.with_fake(post_mode="422-race")
        try:
            proc = self.run_script(fake)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(fake.posts), 1)
        finally:
            fake.close()

    def test_second_invocation_is_preserving_and_total_posts_remain_one(self):
        fake = self.with_fake()
        try:
            self.assertEqual(self.run_script(fake).returncode, 0)
            self.assertEqual(self.run_script(fake).returncode, 0)
            self.assertEqual(len(fake.posts), 1)
        finally:
            fake.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
