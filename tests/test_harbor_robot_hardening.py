#!/usr/bin/env python3
"""Issue #285 runtime blockers (Harbor robot secret preservation + crash-safe
identity) for the per-app `harbor-robot` Request rendered by
`compositions/local/pipeline.yaml`.

Blocker A -- secret preservation: Harbor's `GET /robots/{robot_id}` (the
OBSERVE mapping) never returns the robot's `secret` (confirmed against
goharbor/harbor v2.15.1's `api/v2.0/swagger.yaml` -- the `Robot` schema
declares a `secret` field, but Harbor's own robot-list/get handlers only
ever populate it on the `POST /robots` CREATE response, never on
GET/LIST). provider-http v1.0.14's `KeyInjection.MissingFieldStrategy`
defaults to `"delete"` (`apis/common/secrets_injections.go`), so every
OBSERVE reconcile after the first would delete the already-injected
`secret` key from the Kubernetes Secret the generated CI workflow reads.
This locks `missingFieldStrategy: preserve` on every keyMapping that a
non-CREATE response can leave absent.

Blocker B -- crash-safe identity: the previous OBSERVE mapping resolved the
robot purely via the numeric ID captured from a *cached* CREATE response
(`.response.body.id`). If the Request's `.status` is ever lost (the object
is deleted and recreated after Harbor already accepted the CREATE POST),
that ID is gone and OBSERVE cannot re-discover the robot from its own
declared identity. This locks explicit CUSTOM `isRemovedCheck` /
`expectedResponseCheck` mappings that verify the robot's exact intended
identity (name suffix, level, permissions) from the ID-based GET response,
so a mismatched/uncertain identity is never silently reported as removed
(which would re-trigger CREATE) or synced.

Run:
    python3 -m unittest tests.test_harbor_robot_hardening -v
"""

import base64
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from render_harness import by_kind, make_oxr, render  # noqa: E402


def _harbor_robot_request(appName="hardenapp"):
    items = render(
        {
            "oxr": make_oxr(
                appName=appName,
                gitea={"enabled": True, "visibility": "private", "cicd": True},
            )
        }
    )
    return next(
        i
        for i in by_kind(items, "Request")
        if i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"] == "harbor-robot"
    )


def _run_jq(logic, doc):
    """Evaluate a provider-http CUSTOM-check jq expression against a synthetic
    jqObject (merged forProvider + response) using the real `jq` binary, so
    this test proves the *logic*, not just that a string is present."""
    proc = subprocess.run(
        ["jq", "-c", logic],
        input=json.dumps(doc),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        raise AssertionError("jq failed for logic=%r: %s" % (logic, proc.stderr))
    return json.loads(proc.stdout)


class SecretPreservationTest(unittest.TestCase):
    """Every keyMapping fed by a field Harbor's OBSERVE response omits must
    preserve rather than delete the existing Secret value."""

    @classmethod
    def setUpClass(cls):
        cls.req = _harbor_robot_request()
        cls.injections = cls.req["spec"]["forProvider"]["secretInjectionConfigs"]

    def test_every_key_mapping_preserves_on_missing_field(self):
        self.assertGreaterEqual(len(self.injections), 1)
        for injection in self.injections:
            for mapping in injection["keyMappings"]:
                self.assertEqual(
                    mapping.get("missingFieldStrategy"),
                    "preserve",
                    "%s must preserve (not delete) on a response that omits it"
                    % mapping["secretKey"],
                )

    def test_name_and_secret_keys_still_present(self):
        keys = {m["secretKey"] for i in self.injections for m in i["keyMappings"]}
        self.assertEqual(keys, {"name", "secret"})


class CrashSafeIdentityCheckTest(unittest.TestCase):
    """CUSTOM isRemovedCheck/expectedResponseCheck must verify the robot's
    exact intended identity, never trusting a bare 2xx status."""

    @classmethod
    def setUpClass(cls):
        cls.req = _harbor_robot_request(appName="myapp")
        cls.forProvider = cls.req["spec"]["forProvider"]

    def test_expected_response_check_is_custom(self):
        self.assertEqual(self.forProvider["expectedResponseCheck"]["type"], "CUSTOM")

    def test_is_removed_check_is_custom(self):
        self.assertEqual(self.forProvider["isRemovedCheck"]["type"], "CUSTOM")

    def _expected_logic(self):
        return self.forProvider["expectedResponseCheck"]["logic"]

    def _removed_logic(self):
        return self.forProvider["isRemovedCheck"]["logic"]

    def _matching_robot_body(self):
        return {
            "name": "robot$myapp+myapp-ci",
            "level": "project",
            "permissions": [
                {
                    "kind": "project",
                    "namespace": "myapp",
                    "access": [
                        {"resource": "repository", "action": "push"},
                        {"resource": "repository", "action": "pull"},
                    ],
                }
            ],
        }

    def test_exact_matching_identity_is_synced_and_not_removed(self):
        doc = {"response": {"statusCode": 200, "body": self._matching_robot_body()}}
        self.assertTrue(_run_jq(self._expected_logic(), doc))
        self.assertFalse(_run_jq(self._removed_logic(), doc))

    def test_a_404_is_removed_and_not_synced(self):
        doc = {"response": {"statusCode": 404, "body": "not found"}}
        self.assertFalse(_run_jq(self._expected_logic(), doc))
        self.assertTrue(_run_jq(self._removed_logic(), doc))

    def test_wrong_level_is_not_synced_and_not_removed(self):
        # A response for a different/stale identity must never be silently
        # trusted as synced, but must also never be reported as "removed"
        # (that would re-trigger CREATE against an identity that, in Harbor,
        # actually already exists under a uniqueness constraint).
        body = self._matching_robot_body()
        body["level"] = "system"
        doc = {"response": {"statusCode": 200, "body": body}}
        self.assertFalse(_run_jq(self._expected_logic(), doc))
        self.assertFalse(_run_jq(self._removed_logic(), doc))

    def test_wrong_permissions_is_not_synced_and_not_removed(self):
        body = self._matching_robot_body()
        body["permissions"][0]["access"] = [{"resource": "repository", "action": "push"}]
        doc = {"response": {"statusCode": 200, "body": body}}
        self.assertFalse(_run_jq(self._expected_logic(), doc))
        self.assertFalse(_run_jq(self._removed_logic(), doc))

    def test_extra_permission_action_is_not_synced_and_not_removed(self):
        # Issue #285 review finding: expectedResponseCheck must enforce exact
        # least-privilege permissions -- an extra action (e.g. Harbor RBAC
        # drift granting `repository:delete` alongside push/pull) must never
        # be silently trusted as synced.
        body = self._matching_robot_body()
        body["permissions"][0]["access"].append({"resource": "repository", "action": "delete"})
        doc = {"response": {"statusCode": 200, "body": body}}
        self.assertFalse(_run_jq(self._expected_logic(), doc))
        self.assertFalse(_run_jq(self._removed_logic(), doc))

    def test_different_project_namespace_is_not_synced_and_not_removed(self):
        # Issue #285 review finding (HIGH): expectedResponseCheck no longer
        # reads `r.name` at all -- ApplyResponseDataToSecrets
        # (secretInjectionConfigs below map `.body.name`) redacts it in the
        # very same response *before* this check ever runs (see
        # tests/test_pull_credential_redesign.py for the exact provider-http
        # ordering this proves). `permissions[0].namespace` is never fed to
        # a keyMapping, so it's never redacted, and a Harbor project's
        # namespace already uniquely identifies which app's robot this is --
        # this is now the genuine identity signal a mismatch must trip on.
        body = self._matching_robot_body()
        body["permissions"][0]["namespace"] = "otherapp"
        doc = {"response": {"statusCode": 200, "body": body}}
        self.assertFalse(_run_jq(self._expected_logic(), doc))
        self.assertFalse(_run_jq(self._removed_logic(), doc))

    def test_name_field_no_longer_influences_the_identity_check(self):
        # Belt-and-braces for the HIGH fix: a response whose `name` is
        # completely wrong (or redacted to a placeholder) but whose
        # level/namespace/permissions genuinely match must still be
        # considered synced -- exactly what a post-redaction OBSERVE
        # response looks like in production.
        body = self._matching_robot_body()
        body["name"] = "{{myapp-harbor-robot:myapp:name}}"
        doc = {"response": {"statusCode": 200, "body": body}}
        self.assertTrue(_run_jq(self._expected_logic(), doc))
        self.assertFalse(_run_jq(self._removed_logic(), doc))


class ObserveMappingStillIdBasedTest(unittest.TestCase):
    """Documents *why* the per-app robot's OBSERVE mapping stays ID-based
    (unlike the bootstrap robot's LIST-based OBSERVE in `core`): Harbor's
    `GET /robots` LIST endpoint (goharbor/harbor v2.15.1
    src/server/v2.0/handler/robot.go ListRobot) forces project-scoped
    queries to require a numeric `ProjectID` query keyword that a single
    provider-http HTTP mapping cannot resolve (it would need a prior,
    separate lookup of the project's numeric ID). The bootstrap system-level
    robot has no such requirement (omitting `Level` defaults to system scope,
    ProjectID=0)."""

    def test_observe_mapping_is_still_get_by_cached_id(self):
        req = _harbor_robot_request()
        mappings = req["spec"]["forProvider"]["mappings"]
        observe = next(m for m in mappings if m.get("action") == "OBSERVE")
        self.assertEqual(observe["method"], "GET")
        self.assertIn(".response.body.id", observe["url"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
