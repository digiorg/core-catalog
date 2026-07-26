#!/usr/bin/env python3
"""Automatic immutable image promotion / private pull (Issue #285).

Crossplane is the promotion authority: CI (the generated Gitea Actions
workflow) only ever holds a per-project *push* robot and never an
app-config/kubectl credential. `compositions/local/pipeline.yaml`'s KCL
render instead observes Gitea's main branch HEAD and, per `build.enabled`
service, whether Harbor already has an artifact tagged with that exact
commit SHA -- only then does it pin the Deployment's image to that
artifact's immutable `sha256:` digest (never a mutable tag). A later HEAD
commit whose build hasn't been pushed yet is a routine "pending" state, not
an error: the previously promoted digest (persisted via the function-kcl
dxr status patch into the XR's own `status.services`) must survive it. The
first build of a service has no Deployment at all until its first digest
resolves.

Pull-time image access is deliberately a *separate* identity from CI's push
robot: a pull-only Harbor robot backs a pre-created, properly typed
`kubernetes.io/dockerconfigjson` Secret (provider-kubernetes `Object`,
since provider-http cannot set a Secret's `type` itself) that the
Deployment references via `imagePullSecrets` -- the push credential is
never exposed to the workload.

Run:
    python3 -m unittest tests.test_image_promotion -v
"""

import base64
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from render_harness import by_kind, make_oxr, manifests_of_kind, render  # noqa: E402
from test_provider_http_mapping_contract import (  # noqa: E402
    MappingNotFound,
    resolve_mapping,
)

FORTY_A = "a" * 40
FORTY_C = "c" * 40
DIGEST_B = "sha256:" + "b" * 64
DIGEST_D = "sha256:" + "d" * 64


def _run_jq(logic, doc):
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


def _buildable_service(name="web", image="ignored:placeholder", port=80):
    return {"name": name, "image": image, "port": port, "build": {"enabled": True, "context": "."}}


def _gitea_head_ocd(sha, status_code=200):
    body = json.dumps({"name": "main", "commit": {"id": sha}})
    return {"Resource": {"status": {"response": {"statusCode": status_code, "body": body}}}}


def _harbor_artifact_ocd(digest, tag, status_code=200):
    body = json.dumps({"digest": digest, "tags": [{"name": tag}]})
    return {"Resource": {"status": {"response": {"statusCode": status_code, "body": body}}}}


def _harbor_404_ocd():
    body = json.dumps({"errors": [{"code": "NOT_FOUND", "message": "artifact not found"}]})
    return {"Resource": {"status": {"response": {"statusCode": 404, "body": body}}}}


def _ready_pull_secret_ocd():
    # Issue #285 review finding (typed pull Secret race, see
    # tests/test_pull_secret_race_gating.py): pullRobotRequest only renders
    # once the shell Object (composition-resource-name slug
    # "harbor-pull-secret") reports a Ready=True condition, confirming
    # provider-kubernetes has actually created the typed Secret first. Tests
    # in this file that exercise pullRobotRequest itself (rather than the
    # readiness gate) opt into that state via this fixture.
    return {
        "Resource": {
            "status": {
                "conditions": [
                    {"type": "Synced", "status": "True"},
                    {"type": "Ready", "status": "True"},
                ]
            }
        }
    }


def _pull_robot_ocd(robot_id, status_code=200):
    body = json.dumps(
        {
            "id": robot_id,
            "name": "robot$promoapp+promoapp-pull",
            "level": "project",
            "permissions": [{"kind": "project", "namespace": "promoapp", "access": [{"resource": "repository", "action": "pull"}]}],
        }
    )
    return {"Resource": {"status": {"response": {"statusCode": status_code, "body": body}}}}


def _ready_pull_secret_observed_ocd():
    # Issue #285 review finding (CRITICAL fix): pullSecretObservedObj
    # (composition-resource-name slug "harbor-pull-secret-observed") is the
    # Observe-only provider-kubernetes Object whose `readiness.policy:
    # DeriveFromCelQuery` reports Ready=True only once the destination
    # dockerconfigjson Secret's own live `.data[".dockerconfigjson"]` value
    # has actually moved past the `{auths:{}}` placeholder. Deployments for
    # buildable services are gated on this, not merely on a resolved digest
    # -- see tests/test_pull_credential_redesign.py for the full rationale
    # and the exact CEL query this simulates the effect of.
    return {
        "Resource": {
            "status": {
                "conditions": [
                    {"type": "Synced", "status": "True"},
                    {"type": "Ready", "status": "True"},
                ]
            }
        }
    }


# Every ocds fixture below that expects a promoted Deployment to actually
# render must also carry this -- image-digest promotion and pull-credential
# application are two independent gates that both have to be true.
PULL_CREDENTIAL_READY_OCDS = {
    "harbor-pull-secret": _ready_pull_secret_ocd(),
    "harbor-pull-secret-observed": _ready_pull_secret_observed_ocd(),
}


def _render(appName="promoapp", services=None, ocds=None, prev_status_services=None, gitea=None):
    oxr = make_oxr(
        appName=appName,
        services=services if services is not None else [_buildable_service()],
        gitea=gitea if gitea is not None else {"enabled": True, "visibility": "private", "cicd": True},
    )
    if prev_status_services is not None:
        oxr["status"] = {"services": prev_status_services}
    params = {"oxr": oxr, "ocds": ocds if ocds is not None else {}}
    return render(params)


def _deployment(items, name):
    deployments = manifests_of_kind(items, "Deployment")
    matches = [d for d in deployments if d["metadata"]["name"] == name]
    return matches[0] if matches else None


def _request(items, slug):
    for i in by_kind(items, "Request"):
        if i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"] == slug:
            return i
    return None


def _object(items, slug):
    for i in by_kind(items, "Object"):
        if i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"] == slug:
            return i
    return None


def _status_service(items, name):
    apps = [i for i in items if i.get("kind") == "Application" and i.get("apiVersion") == "platform.digiorg.io/v1alpha1"]
    assert len(apps) == 1, "expected exactly one dxr status-patch item"
    services = apps[0]["status"]["services"]
    matches = [s for s in services if s["name"] == name]
    return matches[0] if matches else None


class InitialPendingTest(unittest.TestCase):
    """First reconcile: nothing observed yet. No Deployment, no fabricated
    digest, but the observing Requests and the pull-secret shell exist.
    Issue #285 review finding (typed pull Secret race): the pull-robot
    Request itself must NOT exist yet -- it assumes the shell Secret already
    exists with the correct type, which on a genuine first reconcile is not
    yet confirmed (see tests/test_pull_secret_race_gating.py)."""

    @classmethod
    def setUpClass(cls):
        cls.items = _render()

    def test_no_deployment_for_the_buildable_service(self):
        self.assertIsNone(_deployment(self.items, "web"))

    def test_service_and_ingress_still_render(self):
        self.assertEqual(len(manifests_of_kind(self.items, "Service")), 1)
        self.assertEqual(len(manifests_of_kind(self.items, "Ingress")), 1)

    def test_gitea_head_request_rendered(self):
        req = _request(self.items, "gitea-head")
        self.assertIsNotNone(req)
        url = req["spec"]["forProvider"]["mappings"][-1]["url"]
        self.assertIn("/repos/DigiOrg/promoapp/branches/main", url)

    def test_no_harbor_artifact_request_yet_no_commit_sha_known(self):
        self.assertIsNone(_request(self.items, "harbor-artifact-web"))

    def test_status_entry_is_present_but_empty(self):
        entry = _status_service(self.items, "web")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["digest"], "")
        self.assertEqual(entry["headSha"], "")
        self.assertEqual(entry["image"], "")

    def test_pull_secret_shell_provisioned_but_pull_robot_not_yet(self):
        self.assertIsNotNone(_object(self.items, "harbor-pull-secret"))
        self.assertIsNone(_request(self.items, "harbor-pull-robot"))


class SuccessfulPromotionTest(unittest.TestCase):
    """Gitea HEAD resolves and Harbor already has a matching artifact: the
    Deployment is pinned to the immutable digest and pulls via the
    pull-only robot's Secret."""

    @classmethod
    def setUpClass(cls):
        cls.items = _render(
            ocds={
                "gitea-head": _gitea_head_ocd(FORTY_A),
                "harbor-artifact-web": _harbor_artifact_ocd(DIGEST_B, FORTY_A),
                **PULL_CREDENTIAL_READY_OCDS,
            }
        )

    def test_deployment_image_is_pinned_to_digest_not_a_tag(self):
        dep = _deployment(self.items, "web")
        self.assertIsNotNone(dep)
        image = dep["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual(image, "digiorg.local/promoapp/web@%s" % DIGEST_B)

    def test_deployment_uses_the_pull_only_secret(self):
        dep = _deployment(self.items, "web")
        self.assertEqual(
            dep["spec"]["template"]["spec"]["imagePullSecrets"], [{"name": "promoapp-harbor-pull"}]
        )

    def test_harbor_artifact_request_targets_the_exact_head_sha_tag(self):
        req = _request(self.items, "harbor-artifact-web")
        self.assertIsNotNone(req)
        url = req["spec"]["forProvider"]["mappings"][-1]["url"]
        self.assertIn("/projects/promoapp/repositories/web/artifacts/%s" % FORTY_A, url)

    def test_status_reflects_the_promoted_digest_and_sha(self):
        entry = _status_service(self.items, "web")
        self.assertEqual(entry["digest"], DIGEST_B)
        self.assertEqual(entry["headSha"], FORTY_A)
        self.assertEqual(entry["image"], "digiorg.local/promoapp/web@%s" % DIGEST_B)


class StaleResponseAndRacePreservesPreviousDigestTest(unittest.TestCase):
    """A newer Gitea HEAD whose build hasn't been pushed yet (Harbor 404s
    the new tag) must never blank out, nor even touch, a digest that was
    already successfully promoted -- the Deployment keeps running the last
    good, fully-verified image."""

    @classmethod
    def setUpClass(cls):
        cls.prev = [{"name": "web", "headSha": FORTY_A, "digest": DIGEST_B, "image": "digiorg.local/promoapp/web@%s" % DIGEST_B}]
        cls.items = _render(
            prev_status_services=cls.prev,
            ocds={
                "gitea-head": _gitea_head_ocd(FORTY_C),
                "harbor-artifact-web": _harbor_404_ocd(),
                **PULL_CREDENTIAL_READY_OCDS,
            },
        )

    def test_status_keeps_the_previous_digest_and_sha(self):
        entry = _status_service(self.items, "web")
        self.assertEqual(entry["digest"], DIGEST_B)
        self.assertEqual(entry["headSha"], FORTY_A)

    def test_deployment_still_runs_the_previously_promoted_image(self):
        dep = _deployment(self.items, "web")
        self.assertIsNotNone(dep)
        image = dep["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual(image, "digiorg.local/promoapp/web@%s" % DIGEST_B)

    def test_harbor_artifact_request_now_targets_the_new_sha_not_the_old_one(self):
        # The observe Request itself always tracks the latest known HEAD --
        # only the *promoted* status/Deployment lag behind until it resolves.
        req = _request(self.items, "harbor-artifact-web")
        url = req["spec"]["forProvider"]["mappings"][-1]["url"]
        self.assertIn("/artifacts/%s" % FORTY_C, url)
        self.assertNotIn("/artifacts/%s" % FORTY_A, url)

    def test_render_succeeded_with_a_live_404_present_not_an_empty_fixture(self):
        # Sanity: the stale-404 response is genuinely live in ocds for this
        # render -- proving the previous-digest preservation above is a
        # deliberate fallback, not an accident of an empty fixture.
        artifact_req = _request(self.items, "harbor-artifact-web")
        self.assertIsNotNone(artifact_req)


class PreviousDigestSurvivesMissingObservationTest(unittest.TestCase):
    """Same preservation guarantee when the observe Requests haven't
    reported anything at all yet this reconcile (ocds entries absent, e.g.
    momentarily lost from the cache) rather than an explicit 404."""

    def test_missing_gitea_head_and_artifact_ocds_keep_previous_status(self):
        prev = [{"name": "web", "headSha": FORTY_A, "digest": DIGEST_B, "image": "x"}]
        items = _render(prev_status_services=prev, ocds={})
        entry = _status_service(items, "web")
        self.assertEqual(entry["digest"], DIGEST_B)
        self.assertEqual(entry["headSha"], FORTY_A)

    def test_deployment_renders_using_the_previous_digest(self):
        prev = [{"name": "web", "headSha": FORTY_A, "digest": DIGEST_B, "image": "x"}]
        items = _render(prev_status_services=prev, ocds=dict(PULL_CREDENTIAL_READY_OCDS))
        dep = _deployment(items, "web")
        self.assertIsNotNone(dep)
        image = dep["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual(image, "digiorg.local/promoapp/web@%s" % DIGEST_B)


class MalformedShaAndDigestRejectedTest(unittest.TestCase):
    """Any deviation from an exact 40-hex commit id / sha256:64-hex digest
    is treated as "not resolved", never partially trusted."""

    def test_malformed_commit_sha_never_promotes(self):
        items = _render(
            ocds={"gitea-head": _gitea_head_ocd("not-a-valid-sha-at-all")}
        )
        entry = _status_service(items, "web")
        self.assertEqual(entry["digest"], "")
        self.assertIsNone(_request(items, "harbor-artifact-web"))

    def test_short_hex_commit_sha_never_promotes(self):
        items = _render(ocds={"gitea-head": _gitea_head_ocd("abc123")})
        entry = _status_service(items, "web")
        self.assertEqual(entry["digest"], "")

    def test_malformed_digest_never_promotes(self):
        items = _render(
            ocds={
                "gitea-head": _gitea_head_ocd(FORTY_A),
                "harbor-artifact-web": _harbor_artifact_ocd("sha256:tooshort", FORTY_A),
            }
        )
        entry = _status_service(items, "web")
        self.assertEqual(entry["digest"], "")
        self.assertIsNone(_deployment(items, "web"))

    def test_digest_without_sha256_prefix_never_promotes(self):
        items = _render(
            ocds={
                "gitea-head": _gitea_head_ocd(FORTY_A),
                "harbor-artifact-web": _harbor_artifact_ocd("b" * 64, FORTY_A),
            }
        )
        entry = _status_service(items, "web")
        self.assertEqual(entry["digest"], "")

    def test_artifact_response_whose_tags_dont_include_the_requested_sha_never_promotes(self):
        # Defends against a stale/mismatched Harbor response being trusted
        # just because it returned 200 with *a* digest.
        items = _render(
            ocds={
                "gitea-head": _gitea_head_ocd(FORTY_A),
                "harbor-artifact-web": _harbor_artifact_ocd(DIGEST_B, "some-other-tag"),
            }
        )
        entry = _status_service(items, "web")
        self.assertEqual(entry["digest"], "")


class DisabledBuildTest(unittest.TestCase):
    """Services that never opt into build.enabled -- or an AppClaim with
    gitea.cicd disabled -- get none of the promotion machinery, and their
    Deployment uses spec.services[].image verbatim, exactly as before this
    feature existed."""

    def test_non_buildable_service_gets_no_promotion_resources(self):
        items = _render(services=[{"name": "web", "image": "external/web:v1", "port": 80}])
        self.assertIsNone(_request(items, "gitea-head"))
        self.assertIsNone(_object(items, "harbor-pull-secret"))
        self.assertIsNone(_request(items, "harbor-pull-robot"))
        apps = [i for i in items if i.get("kind") == "Application"]
        self.assertEqual(len(apps), 0)

    def test_non_buildable_service_deployment_uses_image_verbatim(self):
        items = _render(services=[{"name": "web", "image": "external/web:v1", "port": 80}])
        dep = _deployment(items, "web")
        self.assertIsNotNone(dep)
        self.assertEqual(dep["spec"]["template"]["spec"]["containers"][0]["image"], "external/web:v1")
        self.assertNotIn("imagePullSecrets", dep["spec"]["template"]["spec"])

    def test_build_enabled_but_gitea_cicd_disabled_gets_no_promotion_resources(self):
        items = _render(gitea={"enabled": True, "visibility": "private", "cicd": False})
        self.assertIsNone(_request(items, "gitea-head"))
        self.assertIsNone(_object(items, "harbor-pull-secret"))
        # build.enabled: true means this service's image comes only from the
        # CI/promotion pipeline (XRD: "assumed to reference an already-
        # published, externally-built image" applies only when build is
        # disabled) -- with gitea.cicd off, no such pipeline exists, so
        # there is honestly nothing to run rather than a silent fallback to
        # spec.services[].image.
        self.assertIsNone(_deployment(items, "web"))

    def test_build_enabled_but_gitea_disabled_entirely_gets_no_promotion_resources(self):
        items = _render(gitea={"enabled": False, "visibility": "private", "cicd": True})
        self.assertIsNone(_request(items, "gitea-head"))
        self.assertIsNone(_object(items, "harbor-pull-secret"))


class MultipleServicesIndependentPromotionTest(unittest.TestCase):
    """Each buildable service is promoted (or not) independently -- one
    service's pending build never blocks another's already-resolved one,
    and one shared pull secret/robot serves the whole app."""

    @classmethod
    def setUpClass(cls):
        cls.items = _render(
            services=[
                _buildable_service("web"),
                _buildable_service("api"),
                {"name": "legacy", "image": "external/legacy:v9", "port": 9000},
            ],
            ocds={
                "gitea-head": _gitea_head_ocd(FORTY_A),
                "harbor-artifact-web": _harbor_artifact_ocd(DIGEST_B, FORTY_A),
                # "api" has no artifact response yet -- still pending.
                **PULL_CREDENTIAL_READY_OCDS,
            },
        )

    def test_web_is_promoted_api_is_pending_legacy_is_untouched(self):
        self.assertIsNotNone(_deployment(self.items, "web"))
        self.assertIsNone(_deployment(self.items, "api"))
        self.assertIsNotNone(_deployment(self.items, "legacy"))

    def test_each_buildable_service_has_its_own_harbor_artifact_request(self):
        self.assertIsNotNone(_request(self.items, "harbor-artifact-web"))
        self.assertIsNotNone(_request(self.items, "harbor-artifact-api"))
        self.assertIsNone(_request(self.items, "harbor-artifact-legacy"))

    def test_status_has_independent_entries_for_each_buildable_service(self):
        web_entry = _status_service(self.items, "web")
        api_entry = _status_service(self.items, "api")
        self.assertEqual(web_entry["digest"], DIGEST_B)
        self.assertEqual(api_entry["digest"], "")
        self.assertIsNone(_status_service(self.items, "legacy"))

    def test_a_single_shared_pull_secret_and_robot_serve_the_whole_app(self):
        pull_robot_requests = [
            r
            for r in by_kind(self.items, "Request")
            if r["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"] == "harbor-pull-robot"
        ]
        self.assertEqual(len(pull_robot_requests), 1)
        pull_secret_objects = [
            o
            for o in by_kind(self.items, "Object")
            if o["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"] == "harbor-pull-secret"
        ]
        self.assertEqual(len(pull_secret_objects), 1)
        for name in ("web", "api"):
            dep = _deployment(self.items, name)
            if dep is not None:
                self.assertEqual(
                    dep["spec"]["template"]["spec"]["imagePullSecrets"],
                    [{"name": "promoapp-harbor-pull"}],
                )


class TypedPullSecretTest(unittest.TestCase):
    """The pull secret is pre-created with the correct Kubernetes Secret
    `type`, a validly-shaped placeholder, and a management policy that
    never lets provider-kubernetes fight provider-http's later patch."""

    @classmethod
    def setUpClass(cls):
        cls.items = _render()
        cls.obj = _object(cls.items, "harbor-pull-secret")
        cls.manifest = cls.obj["spec"]["forProvider"]["manifest"]

    def test_secret_type_is_dockerconfigjson(self):
        self.assertEqual(self.manifest["kind"], "Secret")
        self.assertEqual(self.manifest["type"], "kubernetes.io/dockerconfigjson")

    def test_secret_name_matches_what_deployments_reference(self):
        self.assertEqual(self.manifest["metadata"]["name"], "promoapp-harbor-pull")
        self.assertEqual(self.manifest["metadata"]["namespace"], "promoapp")

    def test_placeholder_dockerconfigjson_is_valid_base64_encoded_json(self):
        encoded = self.manifest["data"][".dockerconfigjson"]
        decoded = base64.b64decode(encoded)
        parsed = json.loads(decoded)
        self.assertIn("auths", parsed)

    def test_management_policies_create_and_observe_only_no_update(self):
        # If "Update" (or the "*" wildcard) were included, provider-kubernetes
        # would re-apply this placeholder every reconcile and permanently
        # erase whatever real credential provider-http had just patched in.
        policies = self.obj["spec"]["managementPolicies"]
        self.assertEqual(set(policies), {"Create", "Observe"})
        self.assertNotIn("Update", policies)
        self.assertNotIn("*", policies)


class PushPullSeparationTest(unittest.TestCase):
    """The pull-only robot backing imagePullSecrets is a wholly distinct
    Harbor identity from the per-app CI push robot: different name,
    strictly narrower permissions, and its own separate Secret."""

    @classmethod
    def setUpClass(cls):
        cls.items = _render(ocds={"harbor-pull-secret": _ready_pull_secret_ocd()})
        cls.pull_req = _request(cls.items, "harbor-pull-robot")
        cls.ci_req = _request(cls.items, "harbor-robot")

    def test_pull_robot_is_a_distinct_request_from_the_ci_robot(self):
        self.assertIsNotNone(self.pull_req)
        self.assertIsNotNone(self.ci_req)
        self.assertNotEqual(self.pull_req["metadata"]["name"], self.ci_req["metadata"]["name"])

    def test_pull_robot_body_grants_pull_only_never_push(self):
        body = json.loads(self.pull_req["spec"]["forProvider"]["payload"]["body"])
        self.assertEqual(body["name"], "promoapp-pull")
        seen = {
            (perm["kind"], perm["namespace"], access["resource"], access["action"])
            for perm in body["permissions"]
            for access in perm["access"]
        }
        self.assertEqual(seen, {("project", "promoapp", "repository", "pull")})

    def test_ci_robot_still_retains_both_push_and_pull(self):
        body = json.loads(self.ci_req["spec"]["forProvider"]["payload"]["body"])
        actions = {access["action"] for perm in body["permissions"] for access in perm["access"]}
        self.assertEqual(actions, {"push", "pull"})

    def test_pull_secret_injection_never_references_the_ci_robots_secret_name(self):
        secret_ref = self.pull_req["spec"]["forProvider"]["secretInjectionConfigs"][0]["secretRef"]
        self.assertEqual(secret_ref["name"], "promoapp-harbor-pull-raw")
        self.assertNotEqual(secret_ref["name"], "promoapp-harbor-robot")

    def test_pull_secret_injection_targets_an_intermediate_raw_secret_never_the_typed_one(self):
        # Issue #285 review finding (CRITICAL): provider-http must never
        # construct the final dockerconfigjson itself (see
        # tests/test_pull_credential_redesign.py for the full defect this
        # fixes) -- it only ever extracts the two raw fields into a
        # dedicated intermediate Opaque Secret the sync Job alone reads.
        secretRef = self.pull_req["spec"]["forProvider"]["secretInjectionConfigs"][0]["secretRef"]
        self.assertEqual(secretRef["name"], "promoapp-harbor-pull-raw")
        self.assertNotEqual(secretRef["name"], "promoapp-harbor-pull")
        mappings = self.pull_req["spec"]["forProvider"]["secretInjectionConfigs"][0]["keyMappings"]
        self.assertEqual({m["secretKey"] for m in mappings}, {"name", "secret"})
        for m in mappings:
            self.assertEqual(m["missingFieldStrategy"], "preserve")
            # Each responseJQ must be a bare raw-field path -- never a
            # compound/constructed value (the exact CRITICAL defect: a
            # constructed value defeats provider-http's own redaction and
            # can silently embed a `null` for a field the response omits).
            self.assertRegex(m["responseJQ"], r"^\.body\.[a-zA-Z_][a-zA-Z0-9_]*$")

    def test_pull_robot_expected_response_check_rejects_a_robot_with_extra_push_permission(self):
        logic = self.pull_req["spec"]["forProvider"]["expectedResponseCheck"]["logic"]
        drifted = {
            "response": {
                "statusCode": 200,
                "body": {
                    "name": "robot$promoapp-pull",
                    "level": "project",
                    "permissions": [
                        {
                            "kind": "project",
                            "namespace": "promoapp",
                            "access": [
                                {"resource": "repository", "action": "pull"},
                                {"resource": "repository", "action": "push"},
                            ],
                        }
                    ],
                },
            }
        }
        self.assertFalse(_run_jq(logic, drifted))

    def test_pull_robot_expected_response_check_accepts_the_intended_shape(self):
        logic = self.pull_req["spec"]["forProvider"]["expectedResponseCheck"]["logic"]
        matching = {
            "response": {
                "statusCode": 200,
                "body": {
                    "name": "robot$promoapp-pull",
                    "level": "project",
                    "permissions": [
                        {
                            "kind": "project",
                            "namespace": "promoapp",
                            "access": [{"resource": "repository", "action": "pull"}],
                        }
                    ],
                },
            }
        }
        self.assertTrue(_run_jq(logic, matching))


class ObserveOnlyRequestMappingContractTest(unittest.TestCase):
    """The new observe-only Requests (gitea-head, per-service harbor-artifact)
    still satisfy the same provider-http v1.0.14 mapping-resolution contract
    tests/test_provider_http_mapping_contract.py enforces for every Request
    in this Composition -- a resolvable CREATE and OBSERVE mapping -- even
    though isRemovedCheck/expectedResponseCheck guarantee Create() is never
    actually invoked in practice."""

    @classmethod
    def setUpClass(cls):
        cls.items = _render(
            ocds={
                "gitea-head": _gitea_head_ocd(FORTY_A),
                "harbor-artifact-web": _harbor_artifact_ocd(DIGEST_B, FORTY_A),
            }
        )

    def test_gitea_head_and_harbor_artifact_resolve_create_and_observe(self):
        for slug in ("gitea-head", "harbor-artifact-web"):
            req = _request(self.items, slug)
            self.assertIsNotNone(req, slug)
            mappings = req["spec"]["forProvider"]["mappings"]
            try:
                resolve_mapping(mappings, "CREATE")
                resolve_mapping(mappings, "OBSERVE")
            except MappingNotFound as e:
                self.fail("%s: %s" % (slug, e))

    def test_gitea_head_and_harbor_artifact_never_report_removed_or_out_of_sync(self):
        # Proves the CUSTOM constants directly via the real jq engine, not
        # just that the literal strings "false"/"true" are present.
        for slug in ("gitea-head", "harbor-artifact-web"):
            req = _request(self.items, slug)
            forProvider = req["spec"]["forProvider"]
            self.assertFalse(_run_jq(forProvider["isRemovedCheck"]["logic"], {}))
            self.assertTrue(_run_jq(forProvider["expectedResponseCheck"]["logic"], {}))


class PullSecretSyncCleanStateTest(unittest.TestCase):
    """No pull robot has ever been observed yet (or promotion isn't even
    enabled) -- nothing sync-Job-related may render. Issue #285 review
    finding (CRITICAL fix)."""

    def test_no_job_or_rbac_when_pull_robot_not_yet_observed(self):
        items = _render(ocds=dict(PULL_CREDENTIAL_READY_OCDS))
        self.assertEqual(manifests_of_kind(items, "Job"), [])
        self.assertIsNone(_object(items, "harbor-pull-secret-sync-sa"))
        self.assertIsNone(_object(items, "harbor-pull-secret-sync-role"))
        self.assertIsNone(_object(items, "harbor-pull-secret-sync-rolebinding"))

    def test_disabled_promotion_renders_nothing_sync_related(self):
        items = _render(services=[{"name": "web", "image": "external/web:v1", "port": 80}])
        self.assertEqual(manifests_of_kind(items, "Job"), [])
        self.assertIsNone(_object(items, "harbor-pull-secret-observed"))


class PullSecretSyncReadyStateTest(unittest.TestCase):
    """The pull robot has resolved (a numeric id observed) and the typed
    Secret shell exists -- the sync Job and its least-privilege RBAC must
    render, correctly shaped."""

    @classmethod
    def setUpClass(cls):
        cls.items = _render(
            ocds={**PULL_CREDENTIAL_READY_OCDS, "harbor-pull-robot": _pull_robot_ocd(7)}
        )
        cls.job = manifests_of_kind(cls.items, "Job")[0]

    def test_exactly_one_job_keyed_by_the_non_secret_robot_version(self):
        jobs = manifests_of_kind(self.items, "Job")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["metadata"]["name"], "promoapp-pull-secret-sync-7")
        self.assertEqual(jobs[0]["metadata"]["namespace"], "promoapp")

    def test_job_uses_its_own_least_privilege_service_account(self):
        self.assertEqual(
            self.job["spec"]["template"]["spec"]["serviceAccountName"], "promoapp-pull-secret-sync"
        )

    def test_job_mounts_only_the_two_needed_raw_secret_keys(self):
        volume = self.job["spec"]["template"]["spec"]["volumes"][0]
        self.assertEqual(volume["secret"]["secretName"], "promoapp-harbor-pull-raw")
        self.assertEqual({i["key"] for i in volume["secret"]["items"]}, {"name", "secret"})

    def test_job_never_carries_a_credential_literal_env_or_argv(self):
        rendered = json.dumps(self.job)
        for forbidden in ("s3cr3t", "robot$promoapp"):
            self.assertNotIn(forbidden, rendered)
        env_names = {e["name"] for e in self.job["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env_names, {"REGISTRY", "SECRET_NAME", "NAMESPACE"})

    def test_image_is_pinned_by_digest(self):
        image = self.job["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertIn("@sha256:", image)

    def test_role_is_scoped_to_exactly_the_destination_secret(self):
        role = manifests_of_kind(self.items, "Role")
        matches = [r for r in role if r["metadata"]["name"] == "promoapp-pull-secret-sync"]
        self.assertEqual(len(matches), 1)
        rule = matches[0]["rules"][0]
        self.assertEqual(rule["resourceNames"], ["promoapp-harbor-pull"])
        self.assertEqual(set(rule["verbs"]), {"get", "patch"})
        for forbidden in ("create", "delete", "list", "watch", "*"):
            self.assertNotIn(forbidden, rule["verbs"])

    def test_observed_object_is_observe_only_never_creates_or_updates(self):
        obj = _object(self.items, "harbor-pull-secret-observed")
        self.assertEqual(obj["spec"]["managementPolicies"], ["Observe"])

    def test_observed_object_cel_query_compares_against_the_real_rendered_placeholder(self):
        obj = _object(self.items, "harbor-pull-secret-observed")
        cel = obj["spec"]["readiness"]["celQuery"]
        self.assertEqual(obj["spec"]["readiness"]["policy"], "DeriveFromCelQuery")
        shell_obj = _object(self.items, "harbor-pull-secret")
        placeholder = shell_obj["spec"]["forProvider"]["manifest"]["data"][".dockerconfigjson"]
        self.assertIn(placeholder, cel)
        self.assertIn('".dockerconfigjson"', cel)


class PullSecretSyncRotationTest(unittest.TestCase):
    """A recreated/rotated pull robot (new Harbor-assigned id) must force a
    genuinely new Job -- a Kubernetes Job's spec.template is immutable once
    created, so only a new identity forces a fresh Pod that reads the freshly
    rotated raw credential."""

    def test_new_robot_id_renders_a_differently_named_job(self):
        before = manifests_of_kind(
            _render(ocds={**PULL_CREDENTIAL_READY_OCDS, "harbor-pull-robot": _pull_robot_ocd(7)}), "Job"
        )[0]
        after = manifests_of_kind(
            _render(ocds={**PULL_CREDENTIAL_READY_OCDS, "harbor-pull-robot": _pull_robot_ocd(8)}), "Job"
        )[0]
        self.assertEqual(before["metadata"]["name"], "promoapp-pull-secret-sync-7")
        self.assertEqual(after["metadata"]["name"], "promoapp-pull-secret-sync-8")
        self.assertNotEqual(before["metadata"]["name"], after["metadata"]["name"])

    def test_rbac_identity_stays_stable_across_rotation(self):
        # Only the Job needs to churn -- the RBAC identity/permissions never
        # change, so there's no reason to recreate them every rotation.
        before = _object(
            _render(ocds={**PULL_CREDENTIAL_READY_OCDS, "harbor-pull-robot": _pull_robot_ocd(7)}),
            "harbor-pull-secret-sync-sa",
        )
        after = _object(
            _render(ocds={**PULL_CREDENTIAL_READY_OCDS, "harbor-pull-robot": _pull_robot_ocd(8)}),
            "harbor-pull-secret-sync-sa",
        )
        self.assertEqual(before["metadata"]["name"], after["metadata"]["name"])


class DeploymentGatedOnObservedPullCredentialTest(unittest.TestCase):
    """A resolved image digest alone must never be enough to render a
    Deployment referencing imagePullSecrets -- the *observed* real
    credential (not merely a scheduled sync Job) is a second, independent
    precondition."""

    def test_digest_resolved_but_credential_not_yet_observed_withholds_deployment(self):
        items = _render(
            ocds={
                "gitea-head": _gitea_head_ocd(FORTY_A),
                "harbor-artifact-web": _harbor_artifact_ocd(DIGEST_B, FORTY_A),
                "harbor-pull-secret": _ready_pull_secret_ocd(),
                # harbor-pull-secret-observed deliberately absent/not-ready.
            }
        )
        self.assertIsNone(_deployment(items, "web"))

    def test_digest_resolved_and_credential_observed_ready_renders_deployment(self):
        items = _render(
            ocds={
                "gitea-head": _gitea_head_ocd(FORTY_A),
                "harbor-artifact-web": _harbor_artifact_ocd(DIGEST_B, FORTY_A),
                **PULL_CREDENTIAL_READY_OCDS,
            }
        )
        self.assertIsNotNone(_deployment(items, "web"))


class MultipleServicesShareOneSyncJobTest(unittest.TestCase):
    """One shared sync Job/RBAC set serves every buildable service in the
    app -- never one per service."""

    @classmethod
    def setUpClass(cls):
        cls.items = _render(
            services=[_buildable_service("web"), _buildable_service("api")],
            ocds={
                "gitea-head": _gitea_head_ocd(FORTY_A),
                "harbor-artifact-web": _harbor_artifact_ocd(DIGEST_B, FORTY_A),
                "harbor-artifact-api": _harbor_artifact_ocd(DIGEST_B, FORTY_A),
                "harbor-pull-robot": _pull_robot_ocd(11),
                **PULL_CREDENTIAL_READY_OCDS,
            },
        )

    def test_exactly_one_sync_job_serves_both_services(self):
        self.assertEqual(len(manifests_of_kind(self.items, "Job")), 1)

    def test_exactly_one_rbac_set(self):
        sas = [o for o in manifests_of_kind(self.items, "ServiceAccount") if o["metadata"]["name"] == "promoapp-pull-secret-sync"]
        self.assertEqual(len(sas), 1)

    def test_both_deployments_reference_the_same_pull_secret(self):
        for name in ("web", "api"):
            dep = _deployment(self.items, name)
            self.assertIsNotNone(dep)
            self.assertEqual(
                dep["spec"]["template"]["spec"]["imagePullSecrets"], [{"name": "promoapp-harbor-pull"}]
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
