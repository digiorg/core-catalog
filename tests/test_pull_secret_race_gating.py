#!/usr/bin/env python3
"""Issue #285 review finding: typed pull Secret creation/injection race.

`compositions/local/pipeline.yaml` provisions the per-app Harbor *pull*
credential in two pieces that are deliberately different providers:

  * `pullSecretShellObj` -- a provider-kubernetes `Object` that CREATEs the
    Kubernetes `Secret` itself with `type: kubernetes.io/dockerconfigjson`
    (provider-http's `secretInjectionConfigs` has no field to set a Secret's
    `type`; its `GetOrCreateSecret` fallback would otherwise create a plain
    `Opaque` Secret, breaking kubelet's image-pull credential parsing).
  * `pullRobotRequest` -- a provider-http `Request` whose
    `secretInjectionConfigs` PATCHes the *same* Secret's `.dockerconfigjson`
    key with the real Harbor pull-robot credential once the robot exists.

Both were previously rendered unconditionally in the same `items` list
whenever promotion is enabled. Crossplane reconciles every composed resource
of an XR independently and concurrently -- nothing in the KCL `items` list's
*order* controls which provider's controller actually creates the Secret
first. If provider-http's controller reconciles the Request before
provider-kubernetes' controller has created the typed Secret, provider-http's
own `GetOrCreateSecret` fallback creates it first -- as a plain `Opaque`
Secret, with no `type` field. `Secret.type` is immutable in the Kubernetes
API, so when the `Object` controller then tries to CREATE the same-named
Secret, it collides (409) and, prevented by design from ever CREATE (Update
is deliberately excluded from `managementPolicies` -- see
`tests/test_image_promotion.py::TypedPullSecretTest` -- for the *opposite*
reason: not fighting provider-http's later patch), the Secret is stuck as the
wrong type forever. Both controllers "emitting concurrently" is the race;
relying on `items` list order cannot fix it because Crossplane does not honor
that order for reconciliation.

The fix: `pullRobotRequest` (the *only* piece that assumes the Secret already
exists with the correct type) only renders once `ocds["harbor-pull-secret"]`
-- the composition-resource-name slug (`annotationFor("harbor-pull-secret")`
on `pullSecretShellObj`), never its Kubernetes `metadata.name`
(`${appName}-harbor-pull-secret`) -- reports a `Ready: "True"` condition,
i.e. the shell Object's own controller has confirmed the typed Secret really
exists. The shell Object itself always renders regardless (it must exist
before anything can observe it as ready). This sequences the two controllers
across reconciles (first reconcile: shell only; a later reconcile, once
Crossplane has itself created and reconciled the Object: shell + robot/
injection) instead of ever trying to sequence within one.

Run:
    python3 -m unittest tests.test_pull_secret_race_gating -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from render_harness import by_kind, make_oxr, render  # noqa: E402


def _buildable_service(name="web"):
    return {"name": name, "image": "ignored", "port": 80, "build": {"enabled": True, "context": "."}}


def _condition(cond_type, status, reason="Reconciled"):
    return {
        "type": cond_type,
        "status": status,
        "reason": reason,
        "lastTransitionTime": "2026-07-22T00:00:00Z",
    }


def _object_ocd(conditions):
    return {
        "Resource": {
            "apiVersion": "kubernetes.crossplane.io/v1alpha2",
            "kind": "Object",
            "status": {"conditions": conditions},
        }
    }


READY_PULL_SECRET_OCD = _object_ocd([_condition("Synced", "True"), _condition("Ready", "True")])
PENDING_PULL_SECRET_OCD_SYNCED_ONLY = _object_ocd([_condition("Synced", "True")])
PENDING_PULL_SECRET_OCD_READY_FALSE = _object_ocd(
    [_condition("Synced", "True"), _condition("Ready", "False", reason="Creating")]
)


def _render(appName="pullrace", ocds=None):
    oxr = make_oxr(
        appName=appName,
        services=[_buildable_service()],
        gitea={"enabled": True, "visibility": "private", "cicd": True},
    )
    return render({"oxr": oxr, "ocds": ocds if ocds is not None else {}})


def _pull_robot_request(items):
    for i in by_kind(items, "Request"):
        if i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"] == "harbor-pull-robot":
            return i
    return None


def _pull_secret_shell(items):
    for i in by_kind(items, "Object"):
        if i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"] == "harbor-pull-secret":
            return i
    return None


class CleanFirstReconcileTest(unittest.TestCase):
    """No ocds observation exists yet at all (the very first reconcile of a
    brand-new AppClaim) -- the shell Object must render, the robot/injection
    Request must not."""

    @classmethod
    def setUpClass(cls):
        cls.items = _render(ocds={})

    def test_shell_object_renders(self):
        self.assertIsNotNone(_pull_secret_shell(self.items))

    def test_pull_robot_does_not_render(self):
        self.assertIsNone(_pull_robot_request(self.items))

    def test_shell_object_still_has_correct_type_and_no_update_policy(self):
        # Belt-and-braces: the race fix must not disturb the existing
        # placeholder/typing/management-policy contract.
        obj = _pull_secret_shell(self.items)
        manifest = obj["spec"]["forProvider"]["manifest"]
        self.assertEqual(manifest["type"], "kubernetes.io/dockerconfigjson")
        self.assertNotIn("Update", obj["spec"]["managementPolicies"])


class PendingReconcileTest(unittest.TestCase):
    """The Object exists and has been observed, but hasn't reported
    Ready=True yet (e.g. Synced but still creating, or no conditions at all
    yet) -- still shell only."""

    def test_synced_only_no_ready_condition_yet(self):
        items = _render(ocds={"harbor-pull-secret": PENDING_PULL_SECRET_OCD_SYNCED_ONLY})
        self.assertIsNotNone(_pull_secret_shell(items))
        self.assertIsNone(_pull_robot_request(items))

    def test_explicit_ready_false(self):
        items = _render(ocds={"harbor-pull-secret": PENDING_PULL_SECRET_OCD_READY_FALSE})
        self.assertIsNotNone(_pull_secret_shell(items))
        self.assertIsNone(_pull_robot_request(items))

    def test_empty_conditions_list(self):
        items = _render(ocds={"harbor-pull-secret": _object_ocd([])})
        self.assertIsNone(_pull_robot_request(items))

    def test_malformed_status_never_crashes_and_stays_pending(self):
        # A structurally-odd observed Object (should never happen for a
        # well-typed Kubernetes API object, but the gate must fail closed
        # rather than raise if it ever does).
        items = _render(ocds={"harbor-pull-secret": {"Resource": {"status": "not-a-dict"}}})
        self.assertIsNotNone(_pull_secret_shell(items))
        self.assertIsNone(_pull_robot_request(items))


class ReadyReconcileTest(unittest.TestCase):
    """Once the shell Object reports Ready=True, the robot/injection Request
    must render alongside it."""

    @classmethod
    def setUpClass(cls):
        cls.items = _render(ocds={"harbor-pull-secret": READY_PULL_SECRET_OCD})

    def test_shell_object_still_renders(self):
        self.assertIsNotNone(_pull_secret_shell(self.items))

    def test_pull_robot_now_renders(self):
        req = _pull_robot_request(self.items)
        self.assertIsNotNone(req)

    def test_pull_robot_still_carries_its_secret_injection(self):
        req = _pull_robot_request(self.items)
        injections = req["spec"]["forProvider"]["secretInjectionConfigs"]
        # Issue #285 review finding (CRITICAL fix): the raw robot
        # credentials land in an intermediate Opaque Secret, never directly
        # in the typed dockerconfigjson Secret -- see
        # tests/test_pull_credential_redesign.py.
        self.assertEqual(injections[0]["secretRef"]["name"], "pullrace-harbor-pull-raw")

    def test_ready_condition_among_others_still_counts(self):
        # A real Object typically reports more than just Ready/Synced
        # (e.g. a "LastAsyncOperation" condition on some provider versions)
        # -- the gate must look for a Ready=True condition among others, not
        # assume an exact 2-element shape.
        conditions = [
            _condition("Synced", "True"),
            _condition("LastAsyncOperation", "True", reason="Success"),
            _condition("Ready", "True"),
        ]
        items = _render(ocds={"harbor-pull-secret": _object_ocd(conditions)})
        self.assertIsNotNone(_pull_robot_request(items))


class ResumeReconcileTest(unittest.TestCase):
    """Once ready, repeated (idempotent) reconciles with the same observed
    state must keep rendering the robot/injection Request stably -- and if
    the shell Object's own observed state is ever lost again (e.g. it was
    deleted and recreated, losing its prior Ready condition), the gate must
    correctly fall back to shell-only again rather than assuming readiness
    persists once seen. The gate is driven by the *current* ocds observation
    on every render, never memorized across renders."""

    def test_steady_state_resume_keeps_pull_robot_present(self):
        for _ in range(3):
            items = _render(ocds={"harbor-pull-secret": READY_PULL_SECRET_OCD})
            self.assertIsNotNone(_pull_robot_request(items))

    def test_losing_the_ready_observation_again_reverts_to_shell_only(self):
        ready_items = _render(ocds={"harbor-pull-secret": READY_PULL_SECRET_OCD})
        self.assertIsNotNone(_pull_robot_request(ready_items))

        resumed_without_observation = _render(ocds={})
        self.assertIsNotNone(_pull_secret_shell(resumed_without_observation))
        self.assertIsNone(_pull_robot_request(resumed_without_observation))


class RealisticCompositionResourceNameKeyTest(unittest.TestCase):
    """`ocds` is keyed by each composed resource's
    `krm.kcl.dev/composition-resource-name` annotation value ("slug"), never
    by its Kubernetes `metadata.name`. The shell Object's slug is
    "harbor-pull-secret"; its `metadata.name` is
    "${appName}-harbor-pull-secret" (see `resourceName()` in the
    Composition). A gate that accidentally keyed off the latter would never
    find a match against real function-kcl `ocds` input and would either
    permanently withhold the robot Request or (worse, if the condition
    defaulted open) never actually gate the race at all."""

    def test_keying_by_metadata_name_does_not_satisfy_the_gate(self):
        items = _render(
            ocds={"pullrace-harbor-pull-secret": READY_PULL_SECRET_OCD}
        )
        self.assertIsNone(
            _pull_robot_request(items),
            "the gate must not be satisfied by the Object's metadata.name -- "
            "only the composition-resource-name slug ocds key is real",
        )

    def test_keying_by_the_real_slug_does_satisfy_the_gate(self):
        items = _render(ocds={"harbor-pull-secret": READY_PULL_SECRET_OCD})
        self.assertIsNotNone(_pull_robot_request(items))

    def test_shell_object_itself_is_annotated_with_the_exact_slug(self):
        items = _render(ocds={})
        obj = _pull_secret_shell(items)
        self.assertEqual(
            obj["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"],
            "harbor-pull-secret",
        )
        self.assertNotEqual(obj["metadata"]["name"], "harbor-pull-secret")


if __name__ == "__main__":
    unittest.main(verbosity=2)
