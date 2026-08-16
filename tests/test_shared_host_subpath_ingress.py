#!/usr/bin/env python3
"""Shared-host subpath contract for AppClaim service ingress routes."""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from render_harness import make_oxr, manifests_of_kind, render  # noqa: E402


PLATFORM_HOST = "digiorg.local"
RESERVED_PLATFORM_ROOT_PATHS = {
    "/",
    "/argocd",
    "/backstage",
    "/gitea",
    "/grafana",
    "/harbor",
    "/jaeger",
    "/keycloak",
    "/opencost",
    "/sonarqube",
}


def _ingress_by_name(items, name):
    matches = [
        item
        for item in manifests_of_kind(items, "Ingress")
        if item["metadata"]["name"] == name
    ]
    if len(matches) != 1:
        raise AssertionError("expected exactly one Ingress named %s, got %d" % (name, len(matches)))
    return matches[0]


def _route(ingress):
    rules = ingress["spec"]["rules"]
    if len(rules) != 1 or len(rules[0]["http"]["paths"]) != 1:
        raise AssertionError("generated service Ingress must contain exactly one host rule and path")
    return rules[0], rules[0]["http"]["paths"][0]


class SharedHostSubpathIngressTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = render(
            {
                "oxr": make_oxr(
                    appName="myapp",
                    services=[
                        {
                            "name": "myappapi",
                            "image": "registry.example.com/myapp/api:1",
                            "port": 9950,
                        }
                    ],
                )
            }
        )
        cls.ingress = _ingress_by_name(cls.items, "myappapi-ingress")
        cls.rule, cls.path = _route(cls.ingress)

    def test_uses_the_existing_platform_host(self):
        self.assertEqual(self.rule["host"], PLATFORM_HOST)

    def test_routes_under_the_deterministic_app_service_subpath(self):
        self.assertEqual(self.path["path"], "/myapp-myappapi(/|$)(.*)")
        self.assertEqual(self.path["pathType"], "ImplementationSpecific")
        self.assertEqual(
            self.path["backend"]["service"],
            {"name": "myappapi-svc", "port": {"number": 80}},
        )

    def test_rewrites_the_external_prefix_to_the_backend_root(self):
        annotations = self.ingress["metadata"]["annotations"]
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/use-regex"], "true")
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/rewrite-target"], "/$2")
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/ssl-redirect"], "true")
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/force-ssl-redirect"], "true")

    def test_root_trailing_slash_and_nested_paths_have_stable_backend_paths(self):
        expression = re.compile(self.path["path"])
        rewrite = self.ingress["metadata"]["annotations"]["nginx.ingress.kubernetes.io/rewrite-target"]

        def backend_path(request_path):
            match = expression.fullmatch(request_path)
            self.assertIsNotNone(match, request_path)
            return rewrite.replace("$2", match.group(2))

        self.assertEqual(backend_path("/myapp-myappapi"), "/")
        self.assertEqual(backend_path("/myapp-myappapi/"), "/")
        self.assertEqual(backend_path("/myapp-myappapi/health"), "/health")
        self.assertEqual(backend_path("/myapp-myappapi/api/v1/items"), "/api/v1/items")

    def test_does_not_claim_a_namespaced_tls_secret(self):
        self.assertNotIn(
            "tls",
            self.ingress["spec"],
            "the ingress-nginx platform Ingress remains the single TLS owner for digiorg.local",
        )

    def test_route_does_not_overlap_reserved_platform_root_paths(self):
        route_prefix = self.path["path"].split("(", 1)[0]
        self.assertNotIn(route_prefix, RESERVED_PLATFORM_ROOT_PATHS)
        self.assertTrue(route_prefix.startswith("/myapp-"))


class MultiServiceSharedHostPathTest(unittest.TestCase):
    def test_each_service_gets_a_distinct_path_on_the_same_platform_host(self):
        items = render(
            {
                "oxr": make_oxr(
                    appName="multiapp",
                    services=[
                        {"name": "api", "image": "example/api:1", "port": 8080},
                        {"name": "worker", "image": "example/worker:1", "port": 9090},
                        {"name": "metrics", "image": "example/metrics:1", "port": 9100},
                    ],
                )
            }
        )
        routes = []
        for ingress in manifests_of_kind(items, "Ingress"):
            rule, path = _route(ingress)
            self.assertEqual(rule["host"], PLATFORM_HOST)
            routes.append(path["path"])

        self.assertEqual(
            sorted(routes),
            [
                "/multiapp-api(/|$)(.*)",
                "/multiapp-metrics(/|$)(.*)",
                "/multiapp-worker(/|$)(.*)",
            ],
        )
        self.assertEqual(len(routes), len(set(routes)))


if __name__ == "__main__":
    unittest.main()
