#!/usr/bin/env python3
"""Issue #285 runtime blocker: Gitea repo/workflow drift repair.

Both the `gitea-repo` and `gitea-cicd` Requests previously only had
CREATE (POST) and OBSERVE (GET) mappings -- no UPDATE mapping -- so once
created, neither the repository's visibility nor the generated CI workflow
content could ever be repaired if it drifted from the desired AppClaim
spec (e.g. an operator flips `gitea.visibility`, or a new buildable
service changes the generated workflow).

Validated against go-gitea/gitea v1.26.1's `templates/swagger/v1_json.tmpl`:
  * `PATCH /repos/{owner}/{repo}` (`EditRepoOption`) accepts `private`.
  * `PUT /repos/{owner}/{repo}/contents/{filepath}` (`UpdateFileOptions`)
    requires `content` (base64) and, to update rather than create, the
    file's current `sha` -- returned by `GET .../contents/{filepath}`
    (`ContentsResponse.sha`).

Run:
    python3 -m unittest tests.test_gitea_drift_repair -v
"""

import base64
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from render_harness import (  # noqa: E402
    by_kind,
    make_oxr,
    ready_cicd_context,
    render,
    render_with_ready_scaffold,
)


def _request(slug, appName="driftapp", **kwargs):
    kwargs.setdefault("gitea", {"enabled": True, "visibility": "private", "cicd": True})
    params = {"oxr": make_oxr(appName=appName, **kwargs)}
    if slug == "gitea-cicd":
        params.update(ready_cicd_context(appName))
    items = render_with_ready_scaffold(params) if slug == "gitea-cicd" else render(params)
    return next(
        i
        for i in by_kind(items, "Request")
        if i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"] == slug
    )


def _run_jq(logic, doc):
    proc = subprocess.run(
        ["jq", "-c", logic], input=json.dumps(doc), capture_output=True, text=True, timeout=10
    )
    if proc.returncode != 0:
        raise AssertionError("jq failed for logic=%r: %s" % (logic, proc.stderr))
    return json.loads(proc.stdout)


class RepoVisibilityRepairTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.req = _request("gitea-repo")
        cls.forProvider = cls.req["spec"]["forProvider"]
        cls.mappings = cls.forProvider["mappings"]

    def test_update_mapping_patches_the_repo(self):
        update = next(m for m in self.mappings if m.get("action") == "UPDATE")
        self.assertEqual(update["method"], "PATCH")
        self.assertIn("/repos/DigiOrg/driftapp", update["url"])

    def test_update_body_carries_desired_private_flag(self):
        update = next(m for m in self.mappings if m.get("action") == "UPDATE")
        self.assertIn("private", update["body"])

    def test_expected_response_check_is_custom_and_compares_private(self):
        check = self.forProvider["expectedResponseCheck"]
        self.assertEqual(check["type"], "CUSTOM")
        logic = check["logic"]
        matching = {"response": {"statusCode": 200, "body": {"private": True}}, "payload": {"body": {"private": True}}}
        drifted = {"response": {"statusCode": 200, "body": {"private": False}}, "payload": {"body": {"private": True}}}
        self.assertTrue(_run_jq(logic, matching))
        self.assertFalse(_run_jq(logic, drifted))

    def test_public_visibility_renders_desired_private_false(self):
        req = _request("gitea-repo", gitea={"enabled": True, "visibility": "public", "cicd": True})
        body = json.loads(req["spec"]["forProvider"]["payload"]["body"])
        self.assertFalse(body["private"])


class WorkflowUpdateRepairTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.req = _request(
            "gitea-cicd",
            services=[{"name": "api", "image": "unused", "port": 8080, "build": {"enabled": True}}],
        )
        cls.forProvider = cls.req["spec"]["forProvider"]
        cls.mappings = cls.forProvider["mappings"]
        cls.desired_body = json.loads(cls.forProvider["payload"]["body"])

    def test_update_mapping_is_a_put_using_observed_sha(self):
        update = next(m for m in self.mappings if m.get("action") == "UPDATE")
        self.assertEqual(update["method"], "PUT")
        self.assertIn(".response.body.sha", update["body"])

    def test_update_body_carries_desired_content_branch_message(self):
        update = next(m for m in self.mappings if m.get("action") == "UPDATE")
        for field in ("content", "branch", "message"):
            self.assertIn(field, update["body"])

    def test_expected_response_check_is_custom_and_compares_content(self):
        check = self.forProvider["expectedResponseCheck"]
        self.assertEqual(check["type"], "CUSTOM")
        logic = check["logic"]
        desired_content = self.desired_body["content"]
        matching = {
            "response": {"statusCode": 200, "body": {"sha": "abc123", "content": desired_content, "name": "ci.yaml"}},
            "payload": {"body": self.desired_body},
        }
        drifted = {
            "response": {
                "statusCode": 200,
                "body": {"sha": "abc123", "content": base64.b64encode(b"stale: true\n").decode(), "name": "ci.yaml"},
            },
            "payload": {"body": self.desired_body},
        }
        self.assertTrue(_run_jq(logic, matching))
        self.assertFalse(_run_jq(logic, drifted))


if __name__ == "__main__":
    unittest.main(verbosity=2)
