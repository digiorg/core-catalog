#!/usr/bin/env python3
"""Issue #285 review finding: malformed HTTP 200 Gitea/Harbor response bodies
must never crash the render.

`compositions/local/pipeline.yaml`'s `extractGiteaHeadSha`/`extractHarborDigest`
read `ocds["gitea-head"|"harbor-artifact-<svc>"].Resource.status.response.body`
-- a raw string a real Gitea/Harbor endpoint could return maliciously or by
accident as HTTP 200 with a non-JSON payload (an intermediary proxy's HTML
error page, a truncated response body, a JSON scalar/array instead of the
expected object shape, or a field of the wrong type). Two independent crash
surfaces existed:

  1. `json.decode` on syntactically invalid JSON raises a KCL
     `EvaluationError` (confirmed directly against the pinned kcl-lang/cli
     v0.12.7 binary this repo's render harness uses: `json.decode("not
     json")` aborts the whole program, not just the one expression).
  2. Even syntactically *valid* JSON of the wrong shape crashes the `?.`
     attribute chains built on top of it: KCL's `?.` only guards a *missing*
     key, not a base value of the wrong type -- `parsed?.commit?.id` still
     raises `invalid value 'int' to load attribute 'commit'` if `parsed`
     decoded to e.g. `42` rather than an object.

Because this Composition is a single KCL program (one `function-kcl` pipeline
step producing every resource for the AppClaim -- namespace, RBAC,
Deployments, everything), either crash surface aborts the *entire* render,
not just image promotion, on nothing more than a misbehaving upstream
Gitea/Harbor response.

The fix (`json.validate` gate + `typeof`-checked `asDict`/`asList`/`asStr`
normalization at every hop) must make every case below resolve to "" (not
yet resolved) rather than raising, and -- combined with the existing
previous-digest-preservation behavior (`tests/test_image_promotion.py`) --
a malformed observation must fall back to the previously promoted digest
exactly like a 404 or absent observation already does.

Run:
    python3 -m unittest tests.test_malformed_response_safety -v
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from render_harness import make_oxr, render  # noqa: E402

FORTY_A = "a" * 40
DIGEST_B = "sha256:" + "b" * 64


def _buildable_service(name="web"):
    return {"name": name, "image": "ignored", "port": 80, "build": {"enabled": True, "context": "."}}


def _ocd(status_code, body):
    return {"Resource": {"status": {"response": {"statusCode": status_code, "body": body}}}}


def _render(ocds, prev_status_services=None, appName="malform"):
    oxr = make_oxr(
        appName=appName,
        services=[_buildable_service()],
        gitea={"enabled": True, "visibility": "private", "cicd": True},
    )
    if prev_status_services is not None:
        oxr["status"] = {"services": prev_status_services}
    return render({"oxr": oxr, "ocds": ocds})


def _status_service(items, name):
    apps = [i for i in items if i.get("kind") == "Application" and i.get("apiVersion") == "platform.digiorg.io/v1alpha1"]
    assert len(apps) == 1
    matches = [s for s in apps[0]["status"]["services"] if s["name"] == name]
    return matches[0] if matches else None


MALFORMED_GITEA_HEAD_BODIES = [
    ("html_error_page", "<html><body>502 Bad Gateway</body></html>"),
    ("truncated_json", '{"commit": {"id": "' + "a" * 20),
    ("bare_json_scalar", "42"),
    ("bare_json_array", "[1, 2, 3]"),
    ("json_null", "null"),
    ("commit_wrong_type", json.dumps({"commit": "not-an-object"})),
    ("commit_id_wrong_type", json.dumps({"commit": {"id": 1234567890123}})),
    ("commit_id_is_null", json.dumps({"commit": {"id": None}})),
    ("commit_id_is_object", json.dumps({"commit": {"id": {"nested": True}}})),
]

MALFORMED_HARBOR_ARTIFACT_BODIES = [
    ("html_error_page", "<html>error</html>"),
    ("truncated_json", '{"digest": "sha256:' + "b" * 10),
    ("bare_json_scalar", "true"),
    ("bare_json_array", "[]"),
    ("digest_wrong_type", json.dumps({"digest": 42, "tags": [{"name": FORTY_A}]})),
    ("tags_wrong_type", json.dumps({"digest": DIGEST_B, "tags": "not-a-list"})),
    ("tags_entries_wrong_type", json.dumps({"digest": DIGEST_B, "tags": ["oops", 1, None]})),
    ("tags_entries_missing_name", json.dumps({"digest": DIGEST_B, "tags": [{}]})),
]


class MalformedGiteaHeadNeverCrashesTest(unittest.TestCase):
    """A malformed `gitea-head` 200 response must render successfully (never
    raise) and leave the promotion state unresolved ("")."""

    def test_render_succeeds_and_promotion_stays_pending(self):
        for label, body in MALFORMED_GITEA_HEAD_BODIES:
            with self.subTest(label=label):
                items = _render({"gitea-head": _ocd(200, body)})
                entry = _status_service(items, "web")
                self.assertIsNotNone(entry)
                self.assertEqual(entry["digest"], "")
                self.assertEqual(entry["headSha"], "")

    def test_malformed_head_with_previous_digest_preserves_it(self):
        prev = [{"name": "web", "headSha": FORTY_A, "digest": DIGEST_B, "image": "x"}]
        for label, body in MALFORMED_GITEA_HEAD_BODIES:
            with self.subTest(label=label):
                items = _render({"gitea-head": _ocd(200, body)}, prev_status_services=prev)
                entry = _status_service(items, "web")
                self.assertEqual(entry["digest"], DIGEST_B, "malformed observation must not blank a good digest")
                self.assertEqual(entry["headSha"], FORTY_A)


class MalformedHarborArtifactNeverCrashesTest(unittest.TestCase):
    """A malformed `harbor-artifact-<svc>` 200 response must render
    successfully (never raise) and leave the promotion state unresolved."""

    def test_render_succeeds_and_promotion_stays_pending(self):
        for label, body in MALFORMED_HARBOR_ARTIFACT_BODIES:
            with self.subTest(label=label):
                items = _render(
                    {
                        "gitea-head": _ocd(200, json.dumps({"commit": {"id": FORTY_A}})),
                        "harbor-artifact-web": _ocd(200, body),
                    }
                )
                entry = _status_service(items, "web")
                self.assertIsNotNone(entry)
                self.assertEqual(entry["digest"], "")

    def test_malformed_artifact_with_previous_digest_preserves_it(self):
        prev = [{"name": "web", "headSha": FORTY_A, "digest": DIGEST_B, "image": "x"}]
        for label, body in MALFORMED_HARBOR_ARTIFACT_BODIES:
            with self.subTest(label=label):
                items = _render(
                    {
                        "gitea-head": _ocd(200, json.dumps({"commit": {"id": FORTY_A}})),
                        "harbor-artifact-web": _ocd(200, body),
                    },
                    prev_status_services=prev,
                )
                entry = _status_service(items, "web")
                self.assertEqual(entry["digest"], DIGEST_B)
                self.assertEqual(entry["headSha"], FORTY_A)


class WellFormedResponsesStillResolveTest(unittest.TestCase):
    """Belt-and-braces: the crash-safety hardening must not regress the
    happy path -- a genuinely well-formed pair of responses still promotes."""

    def test_still_promotes_on_clean_input(self):
        items = _render(
            {
                "gitea-head": _ocd(200, json.dumps({"name": "main", "commit": {"id": FORTY_A}})),
                "harbor-artifact-web": _ocd(200, json.dumps({"digest": DIGEST_B, "tags": [{"name": FORTY_A}]})),
            }
        )
        entry = _status_service(items, "web")
        self.assertEqual(entry["digest"], DIGEST_B)
        self.assertEqual(entry["headSha"], FORTY_A)


if __name__ == "__main__":
    unittest.main(verbosity=2)
