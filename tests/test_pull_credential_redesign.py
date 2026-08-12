#!/usr/bin/env python3
"""Issue #285 final review blockers: CRITICAL pull-credential redaction/
overwrite, and the HIGH CI/pull robot identity-check redaction ordering.

Both defects trace to the *same* two facts about provider-http v1.0.14,
verified against the pinned release source
(https://github.com/crossplane-contrib/provider-http/tree/v1.0.14):

  1. `internal/service/request/observe.go IsUpToDate` calls
     `datapatcher.ApplyResponseDataToSecrets` -- which mutates the *same*
     `details.HttpResponse` object a keyMapping's `responseJQ` reads from --
     strictly *before* `determineIfUpToDate` ever evaluates
     `expectedResponseCheck`. Any CUSTOM check logic that reads a field also
     fed to a keyMapping therefore sees the *redacted* value, never the real
     one.

  2. `internal/data-patcher/secret_patcher.go`:
       * `replaceSensitiveValues` only ever redacts the literal
         `valueToPatch` string it just extracted from the *same* raw
         response body it's mutating -- for a *constructed* value (e.g. a
         full `{auths: {...}}` blob built from several raw fields), that
         constructed string never appears verbatim in the real upstream
         response, so nothing is found to redact and the real raw secret
         field(s) survive, unredacted, in `.status.response.body`.
       * `updateSecretData` only honors `missingFieldStrategy` when the
         *whole* extracted value is `nil`. A jq expression that *constructs*
         an object from a field the response omits (jq `null`, not "path
         absent") still successfully parses/marshals to a non-nil string --
         unconditionally overwriting the Secret, `missingFieldStrategy:
         preserve` notwithstanding.

This file reimplements that exact behavior (not a guess) as a small,
faithful Python port -- the same "port the real algorithm, drive it with the
real jq engine" pattern tests/test_provider_http_mapping_contract.py and
tests/test_gitea_actions_secret_observe_semantics.py already use -- and
drives it directly against what `compositions/local/pipeline.yaml` actually
renders.

Run:
    python3 -m unittest tests.test_pull_credential_redesign -v
"""

import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from render_harness import active_namespace_requirement, by_kind, load_pipeline_source, make_oxr, render  # noqa: E402

RAW_FIELD_PATH = re.compile(r"^\.body\.[a-zA-Z_][a-zA-Z0-9_]*$")


def _run_jq_bool(logic, context):
    if shutil.which("jq") is None:  # pragma: no cover - environment guard
        raise unittest.SkipTest("jq binary not available in this environment")
    proc = subprocess.run(
        ["jq", "-e", logic], input=json.dumps(context), capture_output=True, text=True, timeout=10
    )
    out = proc.stdout.strip()
    if out not in ("true", "false"):
        raise AssertionError(
            "jq logic %r did not produce a boolean: stdout=%r stderr=%r" % (logic, proc.stdout, proc.stderr)
        )
    return out == "true"


def extract_value_to_patch(body, response_jq):
    """Faithful port of provider-http's extractValueToPatch
    (internal/data-patcher/secret_patcher.go): only RAW_FIELD_PATH-shaped
    responseJQ (`.body.<field>`) is exercised by this Composition post-fix,
    so a plain dict lookup on the parsed body is the exact behavior for the
    cases this test drives -- `None` is provider-http's "field missing or
    unparsable" outcome either way."""
    m = re.match(r"^\.body\.([a-zA-Z_][a-zA-Z0-9_]*)$", response_jq)
    if not m:
        raise AssertionError("not a raw field path: %r" % response_jq)
    return body.get(m.group(1))


def apply_response_data_to_secret(secret_data, body, key_mappings):
    """Faithful port of updateSecretData's per-mapping missing-field
    handling (secret_patcher.go): a non-None extraction overwrites; a None
    extraction is left untouched under `missingFieldStrategy: preserve`."""
    for mapping in key_mappings:
        value = extract_value_to_patch(body, mapping["responseJQ"])
        if value is not None:
            secret_data[mapping["secretKey"]] = value
        elif mapping["missingFieldStrategy"] == "preserve":
            pass  # existing value, if any, is left exactly as-is
        else:  # pragma: no cover - this Composition never uses another strategy here
            secret_data.pop(mapping["secretKey"], None)
    return secret_data


def redact_response_body_like_provider_http(body, key_mappings, secret_ref):
    """Faithful port of replaceSensitiveValues, restricted to the raw single-
    field mappings this Composition renders post-fix: each mapping whose
    extraction is non-None gets its exact value replaced, in the response
    body itself, by provider-http's real placeholder format
    (`fmt.Sprintf("{{%s:%s:%s}}", secret.Name, secret.Namespace, secretKey)`,
    secret_patcher.go). Mirrors observe.go's real ordering: this mutation
    happens to the *same* body object a later expectedResponseCheck reads."""
    redacted = dict(body)
    for mapping in key_mappings:
        value = extract_value_to_patch(body, mapping["responseJQ"])
        if value is None or value == "":
            continue
        field = mapping["responseJQ"].rsplit(".", 1)[-1]
        placeholder = "{{%s:%s:%s}}" % (secret_ref["name"], secret_ref["namespace"], mapping["secretKey"])
        if redacted.get(field) == value:
            redacted[field] = placeholder
    return redacted


def _pull_robot_request(appName="credapp"):
    items = render(
        {
            "oxr": make_oxr(
                appName=appName,
                services=[{"name": "web", "image": "x", "port": 80, "build": {"enabled": True, "context": "."}}],
                gitea={"enabled": True, "visibility": "private", "cicd": True},
            ),
            "ocds": {"harbor-pull-secret": {"Resource": {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}}},
        }
    )
    return next(
        i
        for i in by_kind(items, "Request")
        if i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"] == "harbor-pull-robot"
    )


def _harbor_robot_request(appName="credapp"):
    items = render(
        {
            "oxr": make_oxr(appName=appName, gitea={"enabled": True, "visibility": "private", "cicd": True}),
            "requiredResources": {"targetNamespace": active_namespace_requirement(appName)},
        }
    )
    return next(
        i
        for i in by_kind(items, "Request")
        if i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"] == "harbor-robot"
    )


class NoCompoundConstructedSecretValuesAnywhereTest(unittest.TestCase):
    """Structural guard for the CRITICAL fix: no secretInjectionConfigs
    keyMapping anywhere in the whole render may construct a compound value
    from multiple response fields -- every responseJQ must be a bare raw
    `.body.<field>` path, which is the only shape provider-http's own
    redaction (replaceSensitiveValues) and per-field missing-value handling
    (updateSecretData) are actually safe for."""

    def test_every_key_mapping_in_the_richest_render_is_a_raw_field_path(self):
        items = render(
            {
                "oxr": make_oxr(
                    appName="structcheck",
                    services=[{"name": "web", "image": "x", "port": 80, "build": {"enabled": True, "context": "."}}],
                    gitea={"enabled": True, "visibility": "public", "cicd": True},
                ),
                "ocds": {
                    "harbor-pull-secret": {"Resource": {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}},
                },
                "requiredResources": {"targetNamespace": active_namespace_requirement("structcheck")},
            }
        )
        checked = 0
        for req in by_kind(items, "Request"):
            for injection in req["spec"]["forProvider"].get("secretInjectionConfigs", []):
                for mapping in injection["keyMappings"]:
                    checked += 1
                    self.assertRegex(
                        mapping["responseJQ"],
                        RAW_FIELD_PATH,
                        "%s: responseJQ %r is not a raw field path -- a constructed value "
                        "defeats provider-http's own redaction and per-field preserve"
                        % (req["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"], mapping["responseJQ"]),
                    )
        self.assertGreaterEqual(checked, 4, "expected harbor-robot (2) + harbor-pull-robot (2) mappings at least")

    def test_no_jq_object_construction_syntax_in_the_composition_source_secret_injection_area(self):
        # Belt-and-braces: the literal defect pattern (`{auths:` built by a
        # responseJQ) must never reappear anywhere in the source.
        source = load_pipeline_source()
        self.assertNotIn('responseJQ = "{auths:', source)
        self.assertNotIn("| tojson\"", source)


class ObserveMissingSecretNeverOverwritesTest(unittest.TestCase):
    """The CRITICAL bug, concretely: an OBSERVE response that omits `secret`
    (true of every OBSERVE after the first CREATE, for both harbor-robot and
    harbor-pull-robot -- neither's ID-based GET ever returns it) must leave
    an already-populated Secret's `secret` key completely untouched -- never
    overwritten with an embedded `null`."""

    def _key_mappings(self, req):
        return req["spec"]["forProvider"]["secretInjectionConfigs"][0]["keyMappings"]

    def test_pull_robot_observe_missing_secret_preserves_existing_value(self):
        req = _pull_robot_request()
        existing = {"name": "old-name", "secret": "previously-captured-s3cr3t"}
        # Harbor's ID-based GET /robots/{id} response: has `name`, never `secret`.
        observe_body = {"id": 42, "name": "robot$credapp+credapp-pull", "level": "project", "permissions": []}
        result = apply_response_data_to_secret(dict(existing), observe_body, self._key_mappings(req))
        self.assertEqual(result["secret"], "previously-captured-s3cr3t")
        self.assertEqual(result["name"], "robot$credapp+credapp-pull")

    def test_harbor_robot_observe_missing_secret_preserves_existing_value(self):
        req = _harbor_robot_request()
        existing = {"name": "old-name", "secret": "previously-captured-s3cr3t"}
        observe_body = {"id": 7, "name": "robot$credapp+credapp-ci", "level": "project", "permissions": []}
        result = apply_response_data_to_secret(dict(existing), observe_body, self._key_mappings(req))
        self.assertEqual(result["secret"], "previously-captured-s3cr3t")

    def test_create_response_with_secret_present_does_update_it(self):
        # Sanity: preserve must not become "never updates" -- a response
        # that genuinely carries the field (Harbor's CREATE response) must
        # still be captured.
        req = _pull_robot_request()
        existing = {"name": "old-name", "secret": "stale-value"}
        create_body = {"id": 42, "name": "robot$credapp+credapp-pull", "secret": "fresh-value"}
        result = apply_response_data_to_secret(dict(existing), create_body, self._key_mappings(req))
        self.assertEqual(result["secret"], "fresh-value")


class RedactionNeverLeaksTheRawSecretIntoStatusTest(unittest.TestCase):
    """The other half of the CRITICAL bug: because each keyMapping now
    extracts a literal raw field, provider-http's own replaceSensitiveValues
    genuinely finds and redacts it in the response body that eventually
    lands in `.status.response` on the Request CR -- unlike the old
    compound-value design, where the constructed blob never matched
    anything in the real body and the raw secret survived unredacted."""

    def test_pull_robot_create_response_secret_is_genuinely_redactable(self):
        req = _pull_robot_request()
        injection = req["spec"]["forProvider"]["secretInjectionConfigs"][0]
        body = {"id": 42, "name": "robot$credapp+credapp-pull", "secret": "s3cr3t-raw-value"}
        redacted = redact_response_body_like_provider_http(body, injection["keyMappings"], injection["secretRef"])
        self.assertNotIn("s3cr3t-raw-value", json.dumps(redacted))
        self.assertIn("{{credapp-harbor-pull-raw:credapp:secret}}", redacted["secret"])

    def test_harbor_robot_create_response_secret_is_genuinely_redactable(self):
        req = _harbor_robot_request()
        injection = req["spec"]["forProvider"]["secretInjectionConfigs"][0]
        body = {"id": 7, "name": "robot$credapp+credapp-ci", "secret": "another-s3cr3t"}
        redacted = redact_response_body_like_provider_http(body, injection["keyMappings"], injection["secretRef"])
        self.assertNotIn("another-s3cr3t", json.dumps(redacted))


class IdentityCheckSurvivesRedactionTest(unittest.TestCase):
    """The HIGH fix, proven against the real ordering: simulate exactly what
    ApplyResponseDataToSecrets does to the response body (redacting `name`)
    *before* feeding the result to the real `jq` engine evaluating
    expectedResponseCheck -- proving the check no longer depends on the now-
    redacted field, for both robots."""

    def _assert_check_survives_name_redaction(self, req, matching_body):
        fp = req["spec"]["forProvider"]
        injection = fp["secretInjectionConfigs"][0]
        redacted_body = redact_response_body_like_provider_http(matching_body, injection["keyMappings"], injection["secretRef"])
        # Sanity: redaction actually changed `name` in this simulation --
        # otherwise this test would prove nothing.
        self.assertNotEqual(redacted_body["name"], matching_body["name"])
        doc = {"response": {"statusCode": 200, "body": redacted_body}}
        self.assertTrue(
            _run_jq_bool(fp["expectedResponseCheck"]["logic"], doc),
            "expectedResponseCheck must still pass against a response whose `name` "
            "field has already been redacted by provider-http's own secret "
            "injection, exactly as it would be on every real OBSERVE",
        )

    def test_harbor_robot_identity_check_survives_name_redaction(self):
        req = _harbor_robot_request()
        body = {
            "id": 7,
            "name": "robot$credapp+credapp-ci",
            "secret": "whatever",
            "level": "project",
            "permissions": [
                {
                    "kind": "project",
                    "namespace": "credapp",
                    "access": [
                        {"resource": "repository", "action": "push"},
                        {"resource": "repository", "action": "pull"},
                    ],
                }
            ],
        }
        self._assert_check_survives_name_redaction(req, body)

    def test_pull_robot_identity_check_survives_name_redaction(self):
        req = _pull_robot_request()
        body = {
            "id": 42,
            "name": "robot$credapp+credapp-pull",
            "secret": "whatever",
            "level": "project",
            "permissions": [{"kind": "project", "namespace": "credapp", "access": [{"resource": "repository", "action": "pull"}]}],
        }
        self._assert_check_survives_name_redaction(req, body)

    def test_neither_check_logic_string_references_the_name_field(self):
        # Belt-and-braces static check: the fix isn't "the redacted value
        # happens to still satisfy a loose check" -- `r.name`/`.body.name`
        # must not appear in the logic at all.
        for req in (_harbor_robot_request(), _pull_robot_request()):
            logic = req["spec"]["forProvider"]["expectedResponseCheck"]["logic"]
            self.assertNotIn("r.name", logic)
            # Word-boundary regex, not substring: ".namespace" legitimately
            # (and safely) appears in this logic and must not false-positive.
            self.assertNotRegex(logic, r"\.name\b")


class PullSecretSyncScriptExecutionTest(unittest.TestCase):
    """Executes the Job's actual generated shell script (extracted verbatim
    from the render -- not a reimplementation) under plain POSIX `sh`, with
    a stub `kubectl` capturing what it would have applied, proving the
    out-of-band dockerconfigjson construction is actually correct -- the
    concrete replacement for provider-http ever doing this itself."""

    @classmethod
    def setUpClass(cls):
        items = render(
            {
                "oxr": make_oxr(
                    appName="scriptexec",
                    services=[{"name": "web", "image": "x", "port": 80, "build": {"enabled": True, "context": "."}}],
                    gitea={"enabled": True, "visibility": "private", "cicd": True},
                ),
                "ocds": {
                    "harbor-pull-secret": {"Resource": {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}},
                    "harbor-pull-robot": {
                        "Resource": {
                            "status": {
                                "response": {
                                    "statusCode": 200,
                                    "body": json.dumps(
                                        {
                                            "id": 5,
                                            "name": "robot$scriptexec+scriptexec-pull",
                                            "level": "project",
                                            "permissions": [
                                                {
                                                    "kind": "project",
                                                    "namespace": "scriptexec",
                                                    "access": [{"resource": "repository", "action": "pull"}],
                                                }
                                            ],
                                        }
                                    ),
                                }
                            }
                        }
                    },
                },
            }
        )
        job_obj = next(
            i
            for i in by_kind(items, "Object")
            if i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"] == "harbor-pull-secret-sync-5"
        )
        cls.manifest = job_obj["spec"]["forProvider"]["manifest"]
        container = cls.manifest["spec"]["template"]["spec"]["containers"][0]
        cls.script = container["command"][2]
        cls.env = {e["name"]: e["value"] for e in container["env"]}
        volume = cls.manifest["spec"]["template"]["spec"]["volumes"][0]
        cls.mount_path = container["volumeMounts"][0]["mountPath"]
        cls.raw_secret_name = volume["secret"]["secretName"]

    def _run_script(self, name_value, secret_value, shell="sh"):
        if shutil.which(shell) is None:  # pragma: no cover - environment guard
            raise unittest.SkipTest("%s not available in this environment" % shell)
        workdir = tempfile.mkdtemp()
        try:
            creds_dir = os.path.join(workdir, "creds")
            os.makedirs(creds_dir)
            with open(os.path.join(creds_dir, "name"), "w") as f:
                f.write(name_value)
            with open(os.path.join(creds_dir, "secret"), "w") as f:
                f.write(secret_value)

            bin_dir = os.path.join(workdir, "bin")
            os.makedirs(bin_dir)
            capture_path = os.path.join(workdir, "applied.json")
            kubectl_stub = os.path.join(bin_dir, "kubectl")
            with open(kubectl_stub, "w") as f:
                f.write("#!/bin/sh\ncat > \"$KUBECTL_CAPTURE_FILE\"\n")
            os.chmod(kubectl_stub, os.stat(kubectl_stub).st_mode | stat.S_IEXEC)

            script_path = os.path.join(workdir, "script.sh")
            with open(script_path, "w") as f:
                f.write(self.script.replace(self.mount_path, creds_dir))

            env = dict(os.environ)
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
            env["KUBECTL_CAPTURE_FILE"] = capture_path
            env.update(self.env)

            proc = subprocess.run([shell, script_path], capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(proc.returncode, 0, "script failed: stdout=%r stderr=%r" % (proc.stdout, proc.stderr))
            self.assertTrue(os.path.isfile(capture_path), "kubectl apply was never invoked")
            with open(capture_path) as f:
                applied = json.load(f)
            return applied, proc
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def test_script_never_hardcodes_the_mount_path_elsewhere_and_reads_from_files(self):
        self.assertIn("NAME=$(cat %s/name)" % self.mount_path, self.script)
        self.assertIn("SECRET=$(cat %s/secret)" % self.mount_path, self.script)
        self.assertEqual(self.raw_secret_name, "scriptexec-harbor-pull-raw")

    def test_produces_a_correct_dockerconfigjson_under_posix_sh(self):
        applied, _ = self._run_script("scriptexec-pull", "plain-secret-value")
        self.assertEqual(applied["apiVersion"], "v1")
        self.assertEqual(applied["kind"], "Secret")
        self.assertEqual(applied["type"], "kubernetes.io/dockerconfigjson")
        self.assertEqual(applied["metadata"]["name"], "scriptexec-harbor-pull")
        self.assertEqual(applied["metadata"]["namespace"], "scriptexec")
        cfg = json.loads(base64.b64decode(applied["data"][".dockerconfigjson"]))
        entry = cfg["auths"]["digiorg.local"]
        self.assertEqual(entry["username"], "scriptexec-pull")
        self.assertEqual(entry["password"], "plain-secret-value")
        self.assertEqual(base64.b64decode(entry["auth"]).decode(), "scriptexec-pull:plain-secret-value")

    def test_correctly_escapes_quotes_and_backslashes_in_the_secret_value(self):
        applied, _ = self._run_script("scriptexec-pull", 'weird"quote\\backslash')
        cfg = json.loads(base64.b64decode(applied["data"][".dockerconfigjson"]))
        entry = cfg["auths"]["digiorg.local"]
        self.assertEqual(entry["password"], 'weird"quote\\backslash')
        self.assertEqual(
            base64.b64decode(entry["auth"]).decode(), 'scriptexec-pull:weird"quote\\backslash'
        )

    def test_runs_identically_under_dash_strict_posix_sh(self):
        # Deliberately excludes any bash/ash-only extension
        # (`${var//search/replace}`) -- proven directly by running under
        # `dash`, which implements neither.
        applied, _ = self._run_script("scriptexec-pull", "dash-checked-value", shell="dash")
        cfg = json.loads(base64.b64decode(applied["data"][".dockerconfigjson"]))
        self.assertEqual(cfg["auths"]["digiorg.local"]["password"], "dash-checked-value")

    def test_credential_value_never_appears_as_a_visible_argv_to_kubectl(self):
        # The stub records nothing about its own invocation args here by
        # design (the real risk this guards is `kubectl` itself receiving
        # the secret on argv) -- assert the script's own source only ever
        # pipes into kubectl, never passes a credential-derived variable as
        # a kubectl argument.
        kubectl_lines = [line for line in self.script.splitlines() if "kubectl" in line]
        self.assertEqual(len(kubectl_lines), 1)
        self.assertRegex(kubectl_lines[0].strip(), r'^printf .*\| kubectl apply -f -$')

    def test_script_never_uses_set_x_or_echoes_the_manifest(self):
        self.assertNotIn("set -x", self.script)
        self.assertNotIn("echo \"$MANIFEST\"", self.script)
        self.assertNotIn("echo $MANIFEST", self.script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
