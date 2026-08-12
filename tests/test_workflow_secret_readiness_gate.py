#!/usr/bin/env python3
"""Fresh CI publication must wait for usable Gitea Actions credentials.

The generated workflow is itself a push to main. Rendering it concurrently with
the Harbor robot and the two Gitea Actions secret Requests can start a run while
`secrets.HARBOR_ROBOT_NAME` is still empty. The workflow Request must therefore
be the final declarative stage: only after the real robot id and both current
Gitea secret descriptions are observed.
"""
import base64
import hashlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from render_harness import by_kind, make_oxr, render  # noqa: E402

ROBOT_ID = 42
ENCODED_NAME = base64.b64encode(b"robot$gateapp+gateapp-ci").decode("ascii")
ENCODED_SECRET = base64.b64encode(b"credential-a").decode("ascii")
FINGERPRINT = hashlib.sha256(f"{ENCODED_NAME}:{ENCODED_SECRET}".encode()).hexdigest()
DESCRIPTION = f"digiorg-managed harbor-robot-version={ROBOT_ID} credential-sha256={FINGERPRINT}"


def _robot_response(robot_id=ROBOT_ID):
    return {
        "Resource": {
            "status": {
                "response": {
                    "statusCode": 200,
                    "body": json.dumps({
                        "id": robot_id,
                        "level": "project",
                        "permissions": [{"kind": "project", "namespace": "gateapp", "access": []}],
                    }),
                }
            }
        }
    }


def _secret_response(name, description=DESCRIPTION, status=200, body=None):
    if body is None:
        body = json.dumps([{"name": name, "description": description, "created_at": "x"}])
    return {"Resource": {"status": {"response": {"statusCode": status, "body": body}}}}


def _required_robot_secret():
    return {
        "targetNamespace": [{
            "Resource": {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": "gateapp"},
                "status": {"phase": "Active"},
            }
        }],
        "harborRobotCredential": [{
            "Resource": {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "gateapp-harbor-robot", "namespace": "gateapp"},
                "data": {"name": ENCODED_NAME, "secret": ENCODED_SECRET},
            }
        }]
    }


def _render(ocds, required=None):
    return render({
        "oxr": make_oxr(
            appName="gateapp",
            gitea={"enabled": True, "visibility": "private", "cicd": True},
            services=[{"name": "api", "image": "unused", "port": 8080, "build": {"enabled": True, "context": "."}}],
        ),
        "ocds": {
            "gitea-repo": {
                "Resource": {
                    "status": {
                        "response": {
                            "statusCode": 200,
                            "body": json.dumps({"full_name": "DigiOrg/gateapp"}),
                        }
                    }
                }
            },
            "ss-o-v1-g1": {
                "Resource": {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
            },
            "ss-c-v1-g1": {
                "Resource": {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
            },
            **ocds,
        },
        "requiredResources": _required_robot_secret() if required is None else required,
    })


def _slugs(items):
    return {
        x.get("metadata", {}).get("annotations", {}).get("krm.kcl.dev/composition-resource-name")
        for x in by_kind(items, "Request")
    }


class WorkflowCredentialGateTest(unittest.TestCase):
    def assert_secret_requests_without_workflow(self, items):
        slugs = _slugs(items)
        self.assertIn("gitea-secret-harbor-robot-name", slugs)
        self.assertIn("gitea-secret-harbor-robot-secret", slugs)
        self.assertIn("harbor-robot", slugs)
        self.assertNotIn("gitea-cicd", slugs)
        self.assertEqual(len([x for x in items if x.get("kind") == "RequiredResources"]), 1)

    def test_initial_render_with_no_observations_withholds_workflow(self):
        self.assert_secret_requests_without_workflow(_render({}))

    def test_robot_without_secret_observations_withholds_workflow(self):
        self.assert_secret_requests_without_workflow(_render({"harbor-robot": _robot_response()}))

    def test_only_one_current_secret_withholds_workflow(self):
        self.assert_secret_requests_without_workflow(_render({
            "harbor-robot": _robot_response(),
            "gitea-secret-harbor-robot-name": _secret_response("HARBOR_ROBOT_NAME"),
        }))

    def test_both_current_secrets_publish_workflow(self):
        items = _render({
            "harbor-robot": _robot_response(),
            "gitea-secret-harbor-robot-name": _secret_response("HARBOR_ROBOT_NAME"),
            "gitea-secret-harbor-robot-secret": _secret_response("HARBOR_ROBOT_SECRET"),
        })
        self.assertIn("gitea-cicd", _slugs(items))

    def test_both_requests_can_observe_the_same_realistic_secret_list(self):
        shared_body = json.dumps([
            {"name": "HARBOR_ROBOT_NAME", "description": DESCRIPTION, "created_at": "x"},
            {"name": "HARBOR_ROBOT_SECRET", "description": DESCRIPTION, "created_at": "y"},
        ])
        items = _render({
            "harbor-robot": _robot_response(),
            "gitea-secret-harbor-robot-name": _secret_response(
                "HARBOR_ROBOT_NAME", body=shared_body
            ),
            "gitea-secret-harbor-robot-secret": _secret_response(
                "HARBOR_ROBOT_SECRET", body=shared_body
            ),
        })
        self.assertIn("gitea-cicd", _slugs(items))

    def test_valid_shared_list_with_malformed_sibling_fails_closed(self):
        mixed_shape_body = json.dumps([
            {"name": "HARBOR_ROBOT_NAME", "description": DESCRIPTION, "created_at": "x"},
            {"name": "HARBOR_ROBOT_SECRET", "description": DESCRIPTION, "created_at": "y"},
            7,
        ])
        self.assert_secret_requests_without_workflow(_render({
            "harbor-robot": _robot_response(),
            "gitea-secret-harbor-robot-name": _secret_response(
                "HARBOR_ROBOT_NAME", body=mixed_shape_body
            ),
            "gitea-secret-harbor-robot-secret": _secret_response(
                "HARBOR_ROBOT_SECRET", body=mixed_shape_body
            ),
        }))

    def test_stale_description_withholds_workflow(self):
        self.assert_secret_requests_without_workflow(_render({
            "harbor-robot": _robot_response(),
            "gitea-secret-harbor-robot-name": _secret_response("HARBOR_ROBOT_NAME", "digiorg-managed harbor-robot-version=41"),
            "gitea-secret-harbor-robot-secret": _secret_response("HARBOR_ROBOT_SECRET"),
        }))

    def test_missing_credential_fingerprint_withholds_workflow(self):
        self.assert_secret_requests_without_workflow(_render({
            "harbor-robot": _robot_response(),
            "gitea-secret-harbor-robot-name": _secret_response("HARBOR_ROBOT_NAME"),
            "gitea-secret-harbor-robot-secret": _secret_response("HARBOR_ROBOT_SECRET"),
        }, required={"targetNamespace": _required_robot_secret()["targetNamespace"]}))

    def test_duplicate_matching_secret_metadata_fails_closed(self):
        duplicate_body = json.dumps([
            {"name": "HARBOR_ROBOT_NAME", "description": DESCRIPTION, "created_at": "x"},
            {"name": "HARBOR_ROBOT_NAME", "description": DESCRIPTION, "created_at": "y"},
        ])
        self.assert_secret_requests_without_workflow(_render({
            "harbor-robot": _robot_response(),
            "gitea-secret-harbor-robot-name": _secret_response(
                "HARBOR_ROBOT_NAME", body=duplicate_body
            ),
            "gitea-secret-harbor-robot-secret": _secret_response("HARBOR_ROBOT_SECRET"),
        }))

    def test_current_plus_stale_duplicate_name_fails_closed(self):
        mixed_body = json.dumps([
            {"name": "HARBOR_ROBOT_NAME", "description": DESCRIPTION, "created_at": "x"},
            {
                "name": "HARBOR_ROBOT_NAME",
                "description": "digiorg-managed harbor-robot-version=41",
                "created_at": "y",
            },
        ])
        self.assert_secret_requests_without_workflow(_render({
            "harbor-robot": _robot_response(),
            "gitea-secret-harbor-robot-name": _secret_response(
                "HARBOR_ROBOT_NAME", body=mixed_body
            ),
            "gitea-secret-harbor-robot-secret": _secret_response("HARBOR_ROBOT_SECRET"),
        }))

    def test_malformed_or_wrong_shaped_secret_observation_fails_closed(self):
        cases = [
            _secret_response("HARBOR_ROBOT_NAME", body="not-json"),
            _secret_response("HARBOR_ROBOT_NAME", body=json.dumps({"name": "HARBOR_ROBOT_NAME", "description": DESCRIPTION})),
            _secret_response("HARBOR_ROBOT_NAME", status=500),
            _secret_response("HARBOR_ROBOT_NAME", body=json.dumps([])),
        ]
        for response in cases:
            with self.subTest(response=response):
                self.assert_secret_requests_without_workflow(_render({
                    "harbor-robot": _robot_response(),
                    "gitea-secret-harbor-robot-name": response,
                    "gitea-secret-harbor-robot-secret": _secret_response("HARBOR_ROBOT_SECRET"),
                }))


if __name__ == "__main__":
    unittest.main(verbosity=2)
