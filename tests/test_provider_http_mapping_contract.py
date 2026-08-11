#!/usr/bin/env python3
"""Behavioral contract test for every `http.crossplane.io/v1alpha2` `Request`
the pipeline Composition renders.

provider-http v1.0.14 resolves *which* `Mapping` entry to use for a given
lifecycle action (CREATE/OBSERVE/UPDATE/REMOVE) via
`internal/service/request/requestmapping.GetMapping`, verified against the
pinned release source
(https://github.com/crossplane-contrib/provider-http/blob/v1.0.14/internal/service/request/requestmapping/mapping.go):

    actionToMathodFactoryMap = {CREATE: POST, OBSERVE: GET, UPDATE: PUT, REMOVE: DELETE}

    GetMapping(params, action):
        method = actionToMathodFactoryMap.get(action, GET)   # unknown action -> GET
        if a mapping with mapping.action == action exists: use it
        elif a mapping with mapping.method == method exists: use it   # fallback
        else: error "<action> or <method> mapping doesn't exist in request, skipping operation"

Critically, a `Mapping` with only `method: HEAD` (or any method outside
POST/GET/PUT/DELETE) is *never* selected as the OBSERVE mapping by the method
fallback, because `getDefaultMethodByAction("OBSERVE")` is hard-coded to
`GET` -- it does not know about HEAD. Such a mapping must set `action:
OBSERVE` explicitly.

Worse, `internal/service/request/observe.go IsUpToDate` treats a
`GetMapping` failure as a hard error (`errFailedToCheckIfUpToDate`), not as
"resource does not exist yet" (that's a *different*, specific sentinel:
`observe.ErrObjectNotFound`, only returned when a mapping *was* found but the
templated request/response indicates absence). A `Request` with no
resolvable OBSERVE mapping at all therefore never reaches `Create()` --
Crossplane's managed-resource reconciler cannot create the external resource
because `Observe()` errors on every reconcile.

This test reimplements that exact selection algorithm (not a guess) and
asserts every rendered `Request` has a resolvable CREATE and OBSERVE mapping
-- the concrete, provider-verified meaning of "idempotent, no duplicate
external resources on resume" for architecture decisions #5/#7.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from render_harness import by_kind, make_oxr, ready_cicd_context, render  # noqa: E402

ACTION_DEFAULT_METHOD = {
    "CREATE": "POST",
    "OBSERVE": "GET",
    "UPDATE": "PUT",
    "REMOVE": "DELETE",
}


class MappingNotFound(AssertionError):
    pass


def resolve_mapping(mappings, action):
    """Faithful port of provider-http v1.0.14's requestmapping.GetMapping."""
    method = ACTION_DEFAULT_METHOD.get(action, "GET")
    for m in mappings:
        if m.get("action") == action:
            return m
    for m in mappings:
        if m.get("method") == method and "action" not in m:
            return m
        if m.get("method") == method and m.get("action") is None:
            return m
    raise MappingNotFound(
        "no mapping resolves for action=%s (fallback method=%s) among %r"
        % (action, method, mappings)
    )


def _richest_render():
    ready = ready_cicd_context("mapcheck")
    return render(
        {
            "oxr": make_oxr(
                appName="mapcheck",
                gitea={"enabled": True, "visibility": "public", "cicd": True},
            ),
            **ready,
        }
    )


class RequestMappingResolutionTest(unittest.TestCase):
    """Every rendered Request must have a CREATE mapping (to provision it)
    and an OBSERVE mapping (so Observe() doesn't hard-error and Crossplane
    can tell created from not-yet-created -- the idempotency precondition)."""

    @classmethod
    def setUpClass(cls):
        cls.requests = by_kind(_richest_render(), "Request")
        cls.assertTrue_ = unittest.TestCase.assertTrue

    def _slug(self, req):
        return req["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"]

    def test_at_least_the_four_expected_requests_are_present(self):
        slugs = {self._slug(r) for r in self.requests}
        self.assertEqual(
            slugs,
            {
                "gitea-repo",
                "gitea-cicd",
                "harbor-project",
                "harbor-robot",
                # Issue #285 blocker #6: push the Harbor robot credentials into
                # Gitea Actions repository secrets so the generated CI workflow
                # can actually authenticate to Harbor.
                "gitea-secret-harbor-robot-name",
                "gitea-secret-harbor-robot-secret",
            },
        )

    def test_every_request_has_a_resolvable_create_mapping(self):
        for req in self.requests:
            mappings = req["spec"]["forProvider"]["mappings"]
            try:
                resolve_mapping(mappings, "CREATE")
            except MappingNotFound as e:
                self.fail("%s: %s" % (self._slug(req), e))

    def test_every_request_has_a_resolvable_observe_mapping(self):
        for req in self.requests:
            mappings = req["spec"]["forProvider"]["mappings"]
            try:
                resolve_mapping(mappings, "OBSERVE")
            except MappingNotFound as e:
                self.fail(
                    "%s: %s -- Observe() will hard-error every reconcile and "
                    "Create() will never run" % (self._slug(req), e)
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
