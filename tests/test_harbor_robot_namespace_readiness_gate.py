#!/usr/bin/env python3
"""The one-shot Harbor robot credential must wait for its target Namespace.

provider-http creates the external Harbor robot before injecting the CREATE-only
credential into a namespaced Secret. If the Namespace and Request are released
in the same render, Harbor creation can succeed while Kubernetes Secret creation
returns NotFound; a later OBSERVE can recover only the robot name, never its
write-only credential. The robot Request must therefore remain withheld until
an exact Active target Namespace is observed through RequiredResources.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from render_harness import by_kind, make_oxr, render  # noqa: E402

APP = "namespacegate"


def _namespace(resource=None):
    return {"targetNamespace": [] if resource is None else [{"Resource": resource}]}


def _resource(name=APP, namespace=None, phase="Active", api_version="v1", kind="Namespace"):
    metadata = {"name": name}
    if namespace is not None:
        metadata["namespace"] = namespace
    return {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": metadata,
        "status": {"phase": phase},
    }


def _render(required):
    return render({
        "oxr": make_oxr(
            appName=APP,
            gitea={"enabled": True, "visibility": "private", "cicd": True},
            services=[{
                "name": "api", "image": "unused", "port": 8080,
                "build": {"enabled": True, "context": "."},
            }],
        ),
        "requiredResources": required,
    })


def _request_slugs(items):
    return {
        item.get("metadata", {}).get("annotations", {}).get(
            "krm.kcl.dev/composition-resource-name"
        )
        for item in by_kind(items, "Request")
    }


class HarborRobotNamespaceReadinessGateTest(unittest.TestCase):
    def assert_prerequisites_without_robot(self, required):
        items = _render(required)
        slugs = _request_slugs(items)
        self.assertIn("harbor-project", slugs)
        self.assertIn("gitea-secret-harbor-robot-name", slugs)
        self.assertIn("gitea-secret-harbor-robot-secret", slugs)
        self.assertNotIn("harbor-robot", slugs)
        self.assertNotIn("gitea-cicd", slugs)
        namespaces = [
            x for x in by_kind(items, "Object")
            if x.get("metadata", {}).get("annotations", {}).get(
                "krm.kcl.dev/composition-resource-name"
            ) == "namespace"
        ]
        self.assertEqual(len(namespaces), 1)
        required_items = [x for x in items if x.get("kind") == "RequiredResources"]
        self.assertEqual(len(required_items), 1)
        requirement = required_items[0]["requirements"]["targetNamespace"]
        self.assertEqual(requirement["apiVersion"], "v1")
        self.assertEqual(requirement["kind"], "Namespace")
        self.assertEqual(requirement["name"], APP)
        self.assertNotIn("namespace", requirement)

    def test_initial_render_withholds_robot_but_keeps_convergence_prerequisites(self):
        self.assert_prerequisites_without_robot({})

    def test_missing_namespace_observation_fails_closed(self):
        self.assert_prerequisites_without_robot(_namespace())

    def test_wrong_identity_or_shape_fails_closed(self):
        malformed = [
            _resource(name="other"),
            _resource(namespace="not-cluster-scoped"),
            _resource(phase="Terminating"),
            _resource(api_version="apps/v1"),
            _resource(kind="Secret"),
            "not-an-object",
        ]
        for value in malformed:
            with self.subTest(value=value):
                self.assert_prerequisites_without_robot(_namespace(value))

    def test_namespace_field_must_be_absent_not_normalized_empty(self):
        for value in (None, "", 0, {}, []):
            resource = _resource()
            resource["metadata"]["namespace"] = value
            with self.subTest(value=value):
                self.assert_prerequisites_without_robot(_namespace(resource))

    def test_deletion_timestamp_must_be_absent_not_normalized_empty(self):
        for value in (None, "", 0, {}, []):
            resource = _resource()
            resource["metadata"]["deletionTimestamp"] = value
            with self.subTest(value=value):
                self.assert_prerequisites_without_robot(_namespace(resource))

    def test_duplicate_namespace_observation_fails_closed(self):
        required = {"targetNamespace": [
            {"Resource": _resource()},
            {"Resource": _resource()},
        ]}
        self.assert_prerequisites_without_robot(required)

    def test_exact_active_namespace_releases_robot_request(self):
        items = _render(_namespace(_resource()))
        slugs = _request_slugs(items)
        self.assertIn("harbor-robot", slugs)
        self.assertNotIn("gitea-cicd", slugs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
