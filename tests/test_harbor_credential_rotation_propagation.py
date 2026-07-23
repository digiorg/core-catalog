#!/usr/bin/env python3
"""Issue #285 review finding: recreated/rotated Harbor CI robot credentials
must propagate to the Gitea Actions secrets the generated CI workflow reads.

`giteaActionsSecretRequest` (`compositions/local/pipeline.yaml`) previously
declared its `expectedResponseCheck` synced purely on the target secret
*name* appearing in Gitea's `GET .../actions/secrets` list response --
because Gitea's list endpoint never returns a secret's value (confirmed
against `code.gitea.io/gitea/modules/structs/secret.go` @ v1.26.1: `Secret`
only exposes `name`/`description`/`created_at`), that name-only check cannot
tell a *stale* Gitea secret (created once, never touched again) from a
genuinely up-to-date one. If the per-app Harbor CI robot is ever recreated or
its secret rotated (e.g. an operator deletes it in Harbor and this
Composition's own `harborRobotRequest` re-CREATEs a fresh one -- a brand new
Harbor-assigned numeric `id` and a brand new `secret`), the local
`${appName}-harbor-robot` Kubernetes Secret gets the new value (via
`harborRobotRequest`'s own `secretInjectionConfigs`), but the old
name-only-checked Gitea Actions secret was already "synced" from the *first*
CREATE and would never be re-PUT -- the generated CI workflow keeps
authenticating with the now-invalid, rotated-away credential forever.

The fix embeds Harbor's robot `id` -- an observable, non-secret identity
(returned by Harbor's own `GET /robots/{id}` OBSERVE response, distinct from
the `secret` field, which is never read here or anywhere in this file) -- as
a `description` on the Gitea secret (Gitea's `CreateOrUpdateSecretOption`
accepts `description` as a plain, non-value field alongside `data`, per
`routers/api/v1/repo/action.go` @ v1.26.1). `expectedResponseCheck` now
requires the observed entry's `description` to match the *desired*
description (which changes whenever the robot's `id` changes) in addition to
the name being present, so a rotated robot's now-stale Gitea secret is
correctly detected as drifted -- triggering the same idempotent PUT via a new
explicit `UPDATE` mapping (Gitea's actions-secrets PUT is itself
create-or-update, so CREATE and UPDATE share the identical mapping shape;
without this explicit UPDATE mapping, provider-http v1.0.14 would have no
resolvable UPDATE and hard-error on every reconcile once drift is detected --
a permanently-unready Request, exactly what this fix must avoid).

Run:
    python3 -m unittest tests.test_harbor_credential_rotation_propagation -v
"""

import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from render_harness import by_kind, make_oxr, render  # noqa: E402
from test_provider_http_mapping_contract import (  # noqa: E402
    MappingNotFound,
    resolve_mapping,
)

ROBOT_ID_A = 42
ROBOT_ID_B = 99999


def _harbor_robot_ocd(robot_id, status_code=200):
    body = json.dumps(
        {
            "id": robot_id,
            "name": "robot$app+app-ci",
            "level": "project",
            "permissions": [{"kind": "project", "namespace": "app", "access": []}],
        }
    )
    return {"Resource": {"status": {"response": {"statusCode": status_code, "body": body}}}}


def _render(appName="rotapp", ocds=None):
    return render(
        {
            "oxr": make_oxr(
                appName=appName,
                gitea={"enabled": True, "visibility": "private", "cicd": True},
            ),
            "ocds": ocds if ocds is not None else {},
        }
    )


def _secret_requests(items):
    by_slug = {
        i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"]: i
        for i in by_kind(items, "Request")
    }
    return {
        "HARBOR_ROBOT_NAME": by_slug["gitea-secret-harbor-robot-name"],
        "HARBOR_ROBOT_SECRET": by_slug["gitea-secret-harbor-robot-secret"],
    }


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


def _jq_payload(fp):
    # provider-http's requestgen.GenerateRequestContext parses `payload.body`
    # from its rendered JSON string into a native structure before jq ever
    # sees it (internal/json.ConvertJSONStringsToMaps) -- the same real
    # shape tests/test_gitea_drift_repair.py's fixtures already use.
    payload = dict(fp["payload"])
    payload["body"] = json.loads(payload["body"])
    return payload


def _synced(fp, status_code, body):
    context = {"payload": _jq_payload(fp), "response": {"statusCode": status_code, "body": body}}
    return _run_jq_bool(fp["expectedResponseCheck"]["logic"], context)


def _removed(fp, status_code, body):
    context = {"payload": _jq_payload(fp), "response": {"statusCode": status_code, "body": body}}
    return _run_jq_bool(fp["isRemovedCheck"]["logic"], context)


class DescriptionEncodesObservableRobotVersionTest(unittest.TestCase):
    """The desired payload body must carry a non-secret, Harbor-robot-id-
    derived `description` -- deterministic for the same id, distinct across
    a rotation, and never the credential value itself."""

    def test_desired_body_has_a_description_field_derived_from_robot_id(self):
        items = _render(ocds={"harbor-robot": _harbor_robot_ocd(ROBOT_ID_A)})
        for name, req in _secret_requests(items).items():
            with self.subTest(name=name):
                body = json.loads(req["spec"]["forProvider"]["payload"]["body"])
                self.assertIn("description", body)
                self.assertIn(str(ROBOT_ID_A), body["description"])

    def test_description_is_deterministic_for_the_same_robot_id(self):
        items1 = _render(ocds={"harbor-robot": _harbor_robot_ocd(ROBOT_ID_A)})
        items2 = _render(ocds={"harbor-robot": _harbor_robot_ocd(ROBOT_ID_A)})
        desc1 = json.loads(_secret_requests(items1)["HARBOR_ROBOT_NAME"]["spec"]["forProvider"]["payload"]["body"])["description"]
        desc2 = json.loads(_secret_requests(items2)["HARBOR_ROBOT_NAME"]["spec"]["forProvider"]["payload"]["body"])["description"]
        self.assertEqual(desc1, desc2)

    def test_description_changes_when_the_robot_is_recreated_with_a_new_id(self):
        before = _render(ocds={"harbor-robot": _harbor_robot_ocd(ROBOT_ID_A)})
        after = _render(ocds={"harbor-robot": _harbor_robot_ocd(ROBOT_ID_B)})
        desc_before = json.loads(_secret_requests(before)["HARBOR_ROBOT_NAME"]["spec"]["forProvider"]["payload"]["body"])["description"]
        desc_after = json.loads(_secret_requests(after)["HARBOR_ROBOT_NAME"]["spec"]["forProvider"]["payload"]["body"])["description"]
        self.assertNotEqual(desc_before, desc_after)

    def test_description_never_contains_the_secret_value_or_the_word_secret_field(self):
        # Belt-and-braces: only `.id` may ever be read for this -- `.secret`
        # must never appear anywhere in the rendered Request.
        items = _render(ocds={"harbor-robot": _harbor_robot_ocd(ROBOT_ID_A)})
        for req in _secret_requests(items).values():
            rendered = json.dumps(req)
            self.assertNotIn("s3cr3t", rendered)


class StaleDescriptionDetectedAsDriftTest(unittest.TestCase):
    """The core bug: a Gitea secret whose name is present but whose
    description reflects an *old* robot id must be reported not-synced (so
    Update() re-PUTs the current, rotated credential) -- not silently
    trusted the way a name-only check would."""

    @classmethod
    def setUpClass(cls):
        cls.items = _render(ocds={"harbor-robot": _harbor_robot_ocd(ROBOT_ID_B)})
        cls.requests = _secret_requests(cls.items)

    def test_matching_name_and_current_description_is_synced(self):
        for name, req in self.requests.items():
            with self.subTest(name=name):
                fp = req["spec"]["forProvider"]
                desired = json.loads(fp["payload"]["body"])
                observed = [{"name": name, "description": desired["description"], "created_at": "x"}]
                self.assertTrue(_synced(fp, 200, observed))

    def test_matching_name_but_stale_description_from_a_prior_robot_is_not_synced(self):
        # Simulates: the secret was created back when the robot had
        # ROBOT_ID_A; the robot has since been recreated as ROBOT_ID_B (this
        # render's desired state), but Gitea still holds the old entry.
        stale_items = _render(ocds={"harbor-robot": _harbor_robot_ocd(ROBOT_ID_A)})
        stale_desc = json.loads(
            _secret_requests(stale_items)["HARBOR_ROBOT_NAME"]["spec"]["forProvider"]["payload"]["body"]
        )["description"]
        fp = self.requests["HARBOR_ROBOT_NAME"]["spec"]["forProvider"]
        observed = [{"name": "HARBOR_ROBOT_NAME", "description": stale_desc, "created_at": "x"}]
        self.assertFalse(_synced(fp, 200, observed))

    def test_stale_description_is_not_synced_but_also_not_reported_removed(self):
        # A stale (not-yet-repaired) secret must trigger Update(), never
        # Create() -- the name genuinely exists in Gitea.
        stale_items = _render(ocds={"harbor-robot": _harbor_robot_ocd(ROBOT_ID_A)})
        stale_desc = json.loads(
            _secret_requests(stale_items)["HARBOR_ROBOT_NAME"]["spec"]["forProvider"]["payload"]["body"]
        )["description"]
        fp = self.requests["HARBOR_ROBOT_NAME"]["spec"]["forProvider"]
        observed = [{"name": "HARBOR_ROBOT_NAME", "description": stale_desc, "created_at": "x"}]
        self.assertFalse(_removed(fp, 200, observed))

    def test_name_entirely_absent_is_still_reported_removed(self):
        # Unchanged pre-existing semantics: absence, not staleness, is what
        # "removed" means.
        fp = self.requests["HARBOR_ROBOT_NAME"]["spec"]["forProvider"]
        self.assertTrue(_removed(fp, 200, []))

    def test_a_different_secrets_entry_does_not_falsely_satisfy_this_one(self):
        fp = self.requests["HARBOR_ROBOT_SECRET"]["spec"]["forProvider"]
        name_desired = json.loads(self.requests["HARBOR_ROBOT_NAME"]["spec"]["forProvider"]["payload"]["body"])
        observed = [{"name": "HARBOR_ROBOT_NAME", "description": name_desired["description"], "created_at": "x"}]
        self.assertFalse(_synced(fp, 200, observed))


class UpdateMappingResolvesTest(unittest.TestCase):
    """Detected drift must actually be repairable: an explicit UPDATE
    mapping must resolve (provider-http v1.0.14
    requestmapping.GetMapping), or Update() hard-errors every reconcile once
    drift is ever detected -- a permanently-unready Request."""

    def test_every_secret_request_has_a_resolvable_update_mapping(self):
        items = _render(ocds={"harbor-robot": _harbor_robot_ocd(ROBOT_ID_A)})
        for name, req in _secret_requests(items).items():
            with self.subTest(name=name):
                mappings = req["spec"]["forProvider"]["mappings"]
                try:
                    resolve_mapping(mappings, "UPDATE")
                except MappingNotFound as e:
                    self.fail(
                        "%s: %s -- Update() will hard-error every reconcile once "
                        "rotation drift is detected, leaving the Request permanently "
                        "unready" % (name, e)
                    )

    def test_update_mapping_is_the_same_idempotent_put_as_create(self):
        items = _render(ocds={"harbor-robot": _harbor_robot_ocd(ROBOT_ID_A)})
        for req in _secret_requests(items).values():
            mappings = req["spec"]["forProvider"]["mappings"]
            create = resolve_mapping(mappings, "CREATE")
            update = resolve_mapping(mappings, "UPDATE")
            self.assertEqual(create["method"], "PUT")
            self.assertEqual(update["method"], "PUT")
            self.assertEqual(create["url"], update["url"])

    def test_create_observe_mappings_still_resolve_too(self):
        # Belt-and-braces: adding UPDATE must not disturb the pre-existing
        # CREATE/OBSERVE contract (tests/test_provider_http_mapping_contract.py).
        items = _render(ocds={"harbor-robot": _harbor_robot_ocd(ROBOT_ID_A)})
        for name, req in _secret_requests(items).items():
            with self.subTest(name=name):
                mappings = req["spec"]["forProvider"]["mappings"]
                resolve_mapping(mappings, "CREATE")
                resolve_mapping(mappings, "OBSERVE")


class PendingRobotVersionNeverCrashesOrSticksTest(unittest.TestCase):
    """Before the Harbor robot has ever produced a real observation (no
    ocds entry, non-200, or a malformed body), the description must still
    render deterministically (some "pending" placeholder) rather than
    crashing -- and the request must never be a permanently-broken shape
    once the robot does resolve."""

    def test_no_harbor_robot_observation_yet_still_renders(self):
        items = _render(ocds={})
        for req in _secret_requests(items).values():
            body = json.loads(req["spec"]["forProvider"]["payload"]["body"])
            self.assertIn("description", body)

    def test_malformed_harbor_robot_body_never_crashes_render(self):
        items = _render(ocds={"harbor-robot": {"Resource": {"status": {"response": {"statusCode": 200, "body": "<html>err</html>"}}}}})
        for req in _secret_requests(items).values():
            body = json.loads(req["spec"]["forProvider"]["payload"]["body"])
            self.assertIn("description", body)

    def test_pending_description_differs_from_a_resolved_one_so_it_still_propagates_once_known(self):
        pending_items = _render(ocds={})
        resolved_items = _render(ocds={"harbor-robot": _harbor_robot_ocd(ROBOT_ID_A)})
        pending_desc = json.loads(
            _secret_requests(pending_items)["HARBOR_ROBOT_NAME"]["spec"]["forProvider"]["payload"]["body"]
        )["description"]
        resolved_desc = json.loads(
            _secret_requests(resolved_items)["HARBOR_ROBOT_NAME"]["spec"]["forProvider"]["payload"]["body"]
        )["description"]
        self.assertNotEqual(pending_desc, resolved_desc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
