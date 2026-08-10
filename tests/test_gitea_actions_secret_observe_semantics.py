#!/usr/bin/env python3
"""Issue #285 blocker (provider-http v1.0.14 Actions-secret OBSERVE is
semantically broken): the two `gitea-secret-*` Requests
(`compositions/local/pipeline.yaml`, `giteaActionsSecretRequest`) only had a
CREATE (PUT) and OBSERVE (GET .../actions/secrets, the *list* endpoint)
mapping and relied entirely on provider-http v1.0.14's DEFAULT checks.

Verified against the pinned release source
(github.com/crossplane-contrib/provider-http @ v1.0.14):

  * `internal/service/request/observe/is_deleted_check.go`
    `defaultIsRemovedResponseCheck.Check` only ever trips on HTTP 404. Gitea's
    `GET /repos/{owner}/{repo}/actions/secrets` returns HTTP 200 even for an
    empty list (confirmed against go-gitea/gitea's
    `routers/api/v1/repo/action.go` `ListActionsSecrets` @ tag v1.26.1 -- the
    appVersion this platform's Gitea chart pins), so the default never
    reports "removed" here.

  * `internal/service/request/observe/is_synced_check.go`
    `defaultIsUpToDateResponseCheck.Check` -> `desiredState` looks for an
    UPDATE mapping; when none exists (our case -- Gitea's actions-secrets PUT
    is itself create-or-update, so there is deliberately no separate UPDATE
    mapping) `isErrorMappingNotFound` short-circuits to `return true, nil`
    *without ever inspecting the response body or status code* --
    `compareResponseAndDesiredState` (the only place that reads
    `details.HttpResponse`) is never reached.

  * `internal/controller/namespaced/request/request.go` `external.Observe`:
    `ResourceExists: false` (which triggers `Create()`) is returned *only*
    when `IsUpToDate` errors with the literal string
    `observe.ErrObjectNotFound` -- exactly what a CUSTOM `isRemovedCheck`
    returning `true` produces (`is_deleted_check.go`
    `customIsRemovedResponseCheck.Check`).

Net effect before the fix: on the very first reconcile, GET returns `200
[]` (empty list) -> default isRemovedCheck says "not removed" (no 404) ->
default expectedResponseCheck says "up to date" (no UPDATE mapping, response
never inspected) -> `ResourceExists: true, ResourceUpToDate: true` ->
`Create()` (the PUT that actually writes the secret) never runs.

The fix adds two forProvider-level CUSTOM JQ checks per Request (gojq,
provider-http's actual JQ engine -- confirmed via its go.mod):

  * `isRemovedCheck`: true (i.e. "removed", the ErrObjectNotFound trigger)
    when the response wasn't a 200, wasn't a JSON array, or the target
    secret name is absent from it -- fails closed toward re-running the
    idempotent CREATE PUT rather than silently treating malformed/error
    responses as "already up to date".
  * `expectedResponseCheck`: true (synced) only when the response was a 200
    JSON array containing the target secret name -- an explicit,
    response-driven up-to-date determination that never relies on the
    dangerous "no UPDATE mapping -> true" default shortcut.

This test reimplements provider-http's exact Observe() decision (not a
guess) and drives it with the real `jq` binary evaluating the exact JQ
strings the Composition renders, against Gitea's real
`GET .../actions/secrets` response schema (a bare JSON array of
`{name, description, created_at}` -- confirmed against
`code.gitea.io/gitea/modules/structs/secret.go` @ v1.26.1; the secret
*value* is never returned by Gitea and must never be read/compared here).

Run:
    python3 -m unittest -v tests.test_gitea_actions_secret_observe_semantics
"""

import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from render_harness import by_kind, make_oxr, render  # noqa: E402

ErrObjectNotFound = "object wasn't found"


class MappingNotFound(AssertionError):
    pass


def _richest_render():
    return render(
        {
            "oxr": make_oxr(
                appName="secretsem",
                gitea={"enabled": True, "visibility": "public", "cicd": True},
            )
        }
    )


def _secret_requests():
    items = _richest_render()
    by_slug = {
        i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"]: i
        for i in by_kind(items, "Request")
    }
    return {
        "HARBOR_ROBOT_NAME": by_slug["gitea-secret-harbor-robot-name"],
        "HARBOR_ROBOT_SECRET": by_slug["gitea-secret-harbor-robot-secret"],
    }


def _run_jq_bool(logic, context):
    """Evaluate a provider-http CUSTOM JQ `logic` string with the real `jq`
    binary (gojq and jq disagree on essentially nothing relevant here --
    both are jq-1.6-superset implementations), against a context object
    shaped the way provider-http's `requestgen.GenerateRequestContext`
    produces it: `forProvider` fields merged with a `response` key. Pinned
    provider-http v1.0.14 recursively converts JSON *object* strings, but its
    `IsJSONString` helper unmarshals only into `map[string]interface{}`;
    top-level array strings such as Gitea's actions-secret list therefore
    remain strings and must be normalized by the rendered checks themselves."""
    if shutil.which("jq") is None:  # pragma: no cover - environment guard
        raise unittest.SkipTest("jq binary not available in this environment")
    proc = subprocess.run(
        ["jq", "-e", logic],
        input=json.dumps(context),
        capture_output=True,
        text=True,
        timeout=10,
    )
    out = proc.stdout.strip()
    if out not in ("true", "false"):
        raise AssertionError(
            "jq logic %r did not produce a boolean (fail-closed violation): "
            "stdout=%r stderr=%r" % (logic, proc.stdout, proc.stderr)
        )
    return out == "true"


def _context(status_code, body, payload_description=None):
    """The subset of GenerateRequestContext's output our logic strings can
    reference: `.response.statusCode`/`.response.body`, plus (Issue #285
    review finding: rotated-credential propagation)
    `.payload.body.description` -- the CUSTOM expectedResponseCheck now
    compares the observed entry's description against this desired value to
    detect a stale (rotated-away) Harbor robot identity, not just presence
    by name."""
    return {
        "payload": {"body": {"description": payload_description}},
        "response": {"statusCode": status_code, "body": body},
    }


def _is_removed(logic, status_code, body):
    return _run_jq_bool(logic, _context(status_code, body))


def _is_up_to_date(logic, status_code, body, payload_description=None):
    return _run_jq_bool(logic, _context(status_code, body, payload_description))


def _observe(is_removed_logic, expected_response_logic, status_code, body, payload_description=None):
    """Faithful port of provider-http v1.0.14's
    `IsUpToDate`/`determineIfRemoved`/`determineIfUpToDate` decision chain,
    restricted to what a CUSTOM/CUSTOM Request exercises. Returns
    (resource_exists, resource_up_to_date)."""
    if _is_removed(is_removed_logic, status_code, body):
        # customIsRemovedResponseCheck.Check returns ErrObjectNotFound;
        # external.Observe maps that literal error to ResourceExists: false.
        return False, False
    synced = _is_up_to_date(expected_response_logic, status_code, body, payload_description)
    return True, synced


GITEA_EMPTY_LIST = []


def _gitea_list_with_both_secrets_current(requests):
    # Issue #285 review finding (rotated-credential propagation): a real
    # "up to date" Gitea response must carry each entry's *current* desired
    # description (sourced from the actual rendered Request, never a
    # hardcoded literal that could silently drift from the real format) --
    # not just a matching name.
    return [
        {
            "name": name,
            "description": json.loads(req["spec"]["forProvider"]["payload"]["body"])["description"],
            "created_at": "2026-07-22T00:00:00Z",
        }
        for name, req in requests.items()
    ]


class CustomChecksArePresentTest(unittest.TestCase):
    """The Request must not rely on provider-http's DEFAULT checks at all."""

    @classmethod
    def setUpClass(cls):
        cls.requests = _secret_requests()

    def test_every_secret_request_declares_a_custom_is_removed_check(self):
        for name, req in self.requests.items():
            with self.subTest(name=name):
                check = req["spec"]["forProvider"]["isRemovedCheck"]
                self.assertEqual(check["type"], "CUSTOM")
                self.assertTrue(check["logic"].strip())

    def test_every_secret_request_declares_a_custom_expected_response_check(self):
        for name, req in self.requests.items():
            with self.subTest(name=name):
                check = req["spec"]["forProvider"]["expectedResponseCheck"]
                self.assertEqual(check["type"], "CUSTOM")
                self.assertTrue(check["logic"].strip())

    def test_logic_references_this_requests_own_secret_name(self):
        for name, req in self.requests.items():
            with self.subTest(name=name):
                fp = req["spec"]["forProvider"]
                self.assertIn(name, fp["isRemovedCheck"]["logic"])
                self.assertIn(name, fp["expectedResponseCheck"]["logic"])

    def test_logic_never_reads_the_secret_value_fields(self):
        # Only `.name` (Gitea's list response never includes the value
        # anyway) -- the logic must never reference `.data`/`.secret`/
        # `.value`-shaped body fields, so no reconcile-loop ever
        # compares/logs a secret value.
        for name, req in self.requests.items():
            fp = req["spec"]["forProvider"]
            for check_name in ("isRemovedCheck", "expectedResponseCheck"):
                logic = fp[check_name]["logic"]
                for forbidden in (".body.secret", ".body.data", ".body.value"):
                    self.assertNotIn(forbidden, logic, "%s.%s must not read %s" % (name, check_name, forbidden))


class EmptyListTriggersCreateTest(unittest.TestCase):
    """The core blocker: an empty list (the real first-reconcile Gitea
    response) must make provider-http run Create(), for each secret name
    independently."""

    @classmethod
    def setUpClass(cls):
        cls.requests = _secret_requests()

    def test_each_secret_independently_triggers_create_on_empty_list(self):
        for name, req in self.requests.items():
            with self.subTest(name=name):
                fp = req["spec"]["forProvider"]
                exists, synced = _observe(
                    fp["isRemovedCheck"]["logic"],
                    fp["expectedResponseCheck"]["logic"],
                    200,
                    GITEA_EMPTY_LIST,
                )
                self.assertFalse(
                    exists,
                    "%s: ResourceExists must be false on an empty secrets list "
                    "so Crossplane calls Create()" % name,
                )


class PresentSecretIsUpToDateTest(unittest.TestCase):
    """Once the CREATE PUT has actually run and the secret shows up in the
    list, the Request must settle as existing+synced -- not loop forever."""

    @classmethod
    def setUpClass(cls):
        cls.requests = _secret_requests()

    def test_each_secret_independently_reports_ready_when_present(self):
        current_list = _gitea_list_with_both_secrets_current(self.requests)
        for name, req in self.requests.items():
            with self.subTest(name=name):
                fp = req["spec"]["forProvider"]
                desired_description = json.loads(fp["payload"]["body"])["description"]
                exists, synced = _observe(
                    fp["isRemovedCheck"]["logic"],
                    fp["expectedResponseCheck"]["logic"],
                    200,
                    current_list,
                    payload_description=desired_description,
                )
                self.assertTrue(exists)
                self.assertTrue(synced)

    def test_string_encoded_array_is_up_to_date_like_provider_http_context(self):
        current_list = _gitea_list_with_both_secrets_current(self.requests)
        encoded_body = json.dumps(current_list)
        for name, req in self.requests.items():
            with self.subTest(name=name):
                fp = req["spec"]["forProvider"]
                desired_description = json.loads(fp["payload"]["body"])["description"]
                exists, synced = _observe(
                    fp["isRemovedCheck"]["logic"],
                    fp["expectedResponseCheck"]["logic"],
                    200,
                    encoded_body,
                    payload_description=desired_description,
                )
                self.assertTrue(exists)
                self.assertTrue(
                    synced,
                    "provider-http v1.0.14 leaves top-level JSON arrays string-encoded",
                )

    def test_a_secret_present_does_not_falsely_satisfy_the_other_secret_name(self):
        # A list containing only HARBOR_ROBOT_NAME must not make the
        # HARBOR_ROBOT_SECRET Request think it, too, is already up to date.
        only_name = [_gitea_list_with_both_secrets_current(self.requests)[0]]
        fp = self.requests["HARBOR_ROBOT_SECRET"]["spec"]["forProvider"]
        desired_description = json.loads(fp["payload"]["body"])["description"]
        exists, _ = _observe(
            fp["isRemovedCheck"]["logic"],
            fp["expectedResponseCheck"]["logic"],
            200,
            only_name,
            payload_description=desired_description,
        )
        self.assertFalse(exists, "HARBOR_ROBOT_SECRET must still be considered missing")


class FailClosedOnErrorOrMalformedResponseTest(unittest.TestCase):
    """404 (repo not found) or a malformed/non-array body must never be
    silently treated as "already up to date" -- they must fail closed
    toward retrying the idempotent CREATE PUT."""

    @classmethod
    def setUpClass(cls):
        cls.requests = _secret_requests()

    def test_404_fails_closed(self):
        for name, req in self.requests.items():
            with self.subTest(name=name):
                fp = req["spec"]["forProvider"]
                exists, _ = _observe(
                    fp["isRemovedCheck"]["logic"], fp["expectedResponseCheck"]["logic"], 404, {"message": "not found"}
                )
                self.assertFalse(exists)

    def test_malformed_non_array_body_fails_closed(self):
        for name, req in self.requests.items():
            with self.subTest(name=name):
                fp = req["spec"]["forProvider"]
                exists, _ = _observe(
                    fp["isRemovedCheck"]["logic"],
                    fp["expectedResponseCheck"]["logic"],
                    200,
                    {"unexpected": "shape"},
                )
                self.assertFalse(exists)

    def test_malformed_string_body_returns_a_boolean_and_fails_closed(self):
        for name, req in self.requests.items():
            with self.subTest(name=name):
                fp = req["spec"]["forProvider"]
                exists, synced = _observe(
                    fp["isRemovedCheck"]["logic"],
                    fp["expectedResponseCheck"]["logic"],
                    200,
                    "not-json",
                )
                self.assertFalse(exists)
                self.assertFalse(synced)

    def test_array_with_non_object_entries_returns_booleans_and_fails_closed(self):
        for name, req in self.requests.items():
            with self.subTest(name=name):
                fp = req["spec"]["forProvider"]
                exists, synced = _observe(
                    fp["isRemovedCheck"]["logic"],
                    fp["expectedResponseCheck"]["logic"],
                    200,
                    ["not-an-object", 7, None],
                )
                self.assertFalse(exists)
                self.assertFalse(synced)

    def test_500_fails_closed(self):
        for name, req in self.requests.items():
            with self.subTest(name=name):
                fp = req["spec"]["forProvider"]
                exists, _ = _observe(
                    fp["isRemovedCheck"]["logic"], fp["expectedResponseCheck"]["logic"], 500, {"message": "boom"}
                )
                self.assertFalse(exists)


if __name__ == "__main__":
    unittest.main(verbosity=2)
