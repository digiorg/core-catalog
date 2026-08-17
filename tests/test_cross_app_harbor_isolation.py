#!/usr/bin/env python3
"""Cross-App Harbor isolation contracts for otherwise identical services."""

import base64
import json
import os
import sys
import unittest

import yaml

sys.path.insert(0, os.path.dirname(__file__))

from render_harness import (  # noqa: E402
    by_kind,
    make_oxr,
    manifests_of_kind,
    ready_cicd_context,
    render,
    render_with_ready_scaffold,
)
from test_image_promotion import (  # noqa: E402
    _gitea_head_ocd,
    _harbor_artifact_ocd,
    PULL_CREDENTIAL_READY_OCDS,
)


ALPHA_APP = "alpha-app"
BETA_APP = "beta-app"
SERVICE = "api"
ALPHA_SHA = "a" * 40
BETA_SHA = "b" * 40
ALPHA_DIGEST = "sha256:" + "c" * 64
BETA_DIGEST = "sha256:" + "d" * 64


def _slug(item):
    return item["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"]


def _request(items, slug):
    return next(item for item in by_kind(items, "Request") if _slug(item) == slug)


def _object(items, slug):
    return next(item for item in by_kind(items, "Object") if _slug(item) == slug)


def _request_body(items, slug):
    return json.loads(_request(items, slug)["spec"]["forProvider"]["payload"]["body"])


def _observe_url(items, slug):
    mappings = _request(items, slug)["spec"]["forProvider"]["mappings"]
    return next(mapping["url"] for mapping in mappings if mapping.get("action") == "OBSERVE")


def _deployment(items):
    return next(
        manifest
        for manifest in manifests_of_kind(items, "Deployment")
        if manifest["metadata"]["name"] == SERVICE
    )


def _status(items):
    xr = next(
        item
        for item in items
        if item.get("apiVersion") == "platform.digiorg.io/v1alpha1"
        and item.get("kind") == "Application"
    )
    return next(entry for entry in xr["status"]["services"] if entry["name"] == SERVICE)


def _workflow(items):
    body = _request_body(items, "gitea-cicd")
    return yaml.safe_load(base64.b64decode(body["content"]).decode())


def _params(app, visibility, sha, digest, robot_id):
    services = [{
        "name": SERVICE,
        "image": "unused",
        "port": 8080,
        "build": {"enabled": True, "context": "."},
    }]
    ready = ready_cicd_context(app, robot_id=robot_id)
    ready["ocds"].update({
        "gitea-head": _gitea_head_ocd(sha),
        "harbor-artifact-api": _harbor_artifact_ocd(digest, sha),
        **PULL_CREDENTIAL_READY_OCDS,
    })
    return {
        "oxr": make_oxr(
            appName=app,
            services=services,
            gitea={"enabled": True, "visibility": visibility, "cicd": True},
        ),
        **ready,
    }


class CrossAppHarborIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.alpha = render_with_ready_scaffold(
            _params(ALPHA_APP, "public", ALPHA_SHA, ALPHA_DIGEST, 101)
        )
        cls.beta = render_with_ready_scaffold(
            _params(BETA_APP, "private", BETA_SHA, BETA_DIGEST, 202)
        )

    def test_project_names_create_bodies_and_observe_urls_are_app_scoped(self):
        for items, app, other in (
            (self.alpha, ALPHA_APP, BETA_APP),
            (self.beta, BETA_APP, ALPHA_APP),
        ):
            request = _request(items, "harbor-project")
            self.assertEqual(request["metadata"]["name"], f"{app}-harbor-project")
            self.assertEqual(_request_body(items, "harbor-project")["project_name"], app)
            observe_url = _observe_url(items, "harbor-project")
            self.assertEqual(
                observe_url,
                f'(.payload.baseUrl + "/projects?project_name={app}")',
            )
            self.assertNotIn(other, observe_url)

    def test_artifact_urls_are_exact_and_never_cross_app(self):
        for items, app, sha, other in (
            (self.alpha, ALPHA_APP, ALPHA_SHA, BETA_APP),
            (self.beta, BETA_APP, BETA_SHA, ALPHA_APP),
        ):
            url = _observe_url(items, "harbor-artifact-api")
            self.assertEqual(
                url,
                f'(.payload.baseUrl + "/projects/{app}/repositories/api/artifacts/{sha}")',
            )
            self.assertNotIn(other, url)

    def test_workflow_image_targets_are_app_and_service_scoped(self):
        for items, app, other in (
            (self.alpha, ALPHA_APP, BETA_APP),
            (self.beta, BETA_APP, ALPHA_APP),
        ):
            workflow = _workflow(items)
            runs = [step.get("run", "") for step in workflow["jobs"]["build-api"]["steps"]]
            rendered = "\n".join(runs)
            self.assertIn(f"digiorg.local/{app}/api':${{{{ gitea.sha }}}}", rendered)
            self.assertNotIn(f"digiorg.local/{other}/", rendered)

    def test_push_and_pull_robot_namespaces_are_separate_per_app(self):
        for items, app, other in (
            (self.alpha, ALPHA_APP, BETA_APP),
            (self.beta, BETA_APP, ALPHA_APP),
        ):
            push = _request_body(items, "harbor-robot")
            pull = _request_body(items, "harbor-pull-robot")
            self.assertEqual(push["permissions"][0]["namespace"], app)
            self.assertEqual(pull["permissions"][0]["namespace"], app)
            self.assertEqual(
                {access["action"] for access in push["permissions"][0]["access"]},
                {"push", "pull"},
            )
            self.assertEqual(
                {access["action"] for access in pull["permissions"][0]["access"]},
                {"pull"},
            )
            self.assertNotIn(other, json.dumps([push, pull]))

    def test_pull_secrets_are_namespaced_and_app_specific(self):
        for items, app in ((self.alpha, ALPHA_APP), (self.beta, BETA_APP)):
            secret = _object(items, "harbor-pull-secret")["spec"]["forProvider"]["manifest"]
            self.assertEqual(secret["metadata"], {
                "name": f"{app}-harbor-pull",
                "namespace": app,
            })
            pull_injection = _request(items, "harbor-pull-robot")["spec"]["forProvider"][
                "secretInjectionConfigs"
            ][0]["secretRef"]
            self.assertEqual(pull_injection, {
                "name": f"{app}-harbor-pull-raw",
                "namespace": app,
            })
            self.assertEqual(
                _deployment(items)["spec"]["template"]["spec"]["imagePullSecrets"],
                [{"name": f"{app}-harbor-pull"}],
            )

    def test_deployment_and_status_are_pinned_to_each_apps_digest(self):
        for items, app, sha, digest, other_digest in (
            (self.alpha, ALPHA_APP, ALPHA_SHA, ALPHA_DIGEST, BETA_DIGEST),
            (self.beta, BETA_APP, BETA_SHA, BETA_DIGEST, ALPHA_DIGEST),
        ):
            image = f"digiorg.local/{app}/api@{digest}"
            self.assertEqual(
                _deployment(items)["spec"]["template"]["spec"]["containers"][0]["image"],
                image,
            )
            self.assertEqual(_status(items), {
                "name": SERVICE,
                "headSha": sha,
                "digest": digest,
                "image": image,
            })
            self.assertNotIn(other_digest, image)

    def test_public_private_metadata_does_not_bleed_across_apps(self):
        self.assertEqual(
            _request_body(self.alpha, "harbor-project")["metadata"],
            {"public": "true"},
        )
        self.assertEqual(
            _request_body(self.beta, "harbor-project")["metadata"],
            {"public": "false"},
        )

    def test_alpha_digest_cannot_promote_beta_from_a_mismatched_observation(self):
        params = _params(BETA_APP, "private", BETA_SHA, BETA_DIGEST, 303)
        params["ocds"]["harbor-artifact-api"] = _harbor_artifact_ocd(
            ALPHA_DIGEST, ALPHA_SHA
        )
        items = render(params)

        self.assertEqual(_status(items)["digest"], "")
        self.assertFalse(any(
            deployment["metadata"]["name"] == SERVICE
            for deployment in manifests_of_kind(items, "Deployment")
        ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
