#!/usr/bin/env python3
"""Lifecycle and least-privilege contracts for the Harbor pull-secret sync Job."""

import unittest
from render_harness import make_oxr, manifests_of_kind, render


class PullSecretSyncLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        oxr = make_oxr(
            services=[{"name": "web", "image": "placeholder", "port": 8080, "build": {"enabled": True, "context": "."}}],
            gitea={"enabled": True, "cicd": True, "visibility": "private"},
        )
        cls.items = render(
            {
                "oxr": oxr,
                "ocds": {
                    "harbor-pull-secret": {"Resource": {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}},
                    "harbor-pull-robot": {"Resource": {"status": {"response": {"statusCode": 200, "body": '{"id":42}'}}}},
                },
            }
        )

    def test_completed_versioned_job_is_not_ttl_deleted_and_recreated_forever(self):
        jobs = manifests_of_kind(self.items, "Job")
        sync_job = next(item for item in jobs if "pull-secret-sync" in item["metadata"]["name"])
        spec = sync_job["spec"]
        self.assertNotIn("ttlSecondsAfterFinished", spec)

    def test_role_uses_only_get_and_patch_for_kubectl_apply(self):
        roles = manifests_of_kind(self.items, "Role")
        role = next(item for item in roles if "pull-secret-sync" in item["metadata"]["name"])
        rule = role["rules"][0]
        self.assertEqual(rule["verbs"], ["get", "patch"])
        self.assertEqual(len(rule["resourceNames"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
