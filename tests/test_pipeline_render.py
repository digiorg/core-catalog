#!/usr/bin/env python3
"""Render tests for the single deterministic AppClaim pipeline Composition
(Issue digiorg/core#285, architecture decision #3: "one deterministic pipeline
Composition with conditional and iterative resource generation").

Run:
    python3 -m unittest discover -s tests -p 'test_*.py'
"""

import base64
import json
import os
import re
import sys
import unittest

import yaml

sys.path.insert(0, os.path.dirname(__file__))

from render_harness import (  # noqa: E402
    PIPELINE_COMPOSITION,
    by_kind,
    load_pipeline_source,
    make_oxr,
    manifests_of_kind,
    render,
)


class BaseAlwaysPresentTest(unittest.TestCase):
    """Base namespace/RBAC/network-policy resources are rendered for every
    AppClaim regardless of which optional capabilities are enabled."""

    @classmethod
    def setUpClass(cls):
        cls.items = render({"oxr": make_oxr(appName="myapp", team="platform-team", size="S")})

    def test_namespace_rendered(self):
        namespaces = manifests_of_kind(self.items, "Namespace")
        self.assertEqual(len(namespaces), 1)
        ns = namespaces[0]
        self.assertEqual(ns["metadata"]["name"], "myapp")
        self.assertEqual(
            ns["metadata"]["labels"]["platform.digiorg.io/team"], "platform-team"
        )
        self.assertEqual(ns["metadata"]["labels"]["platform.digiorg.io/size"], "S")

    def test_serviceaccount_role_rolebinding_networkpolicy_rendered(self):
        self.assertEqual(len(manifests_of_kind(self.items, "ServiceAccount")), 1)
        self.assertEqual(len(manifests_of_kind(self.items, "Role")), 1)
        self.assertEqual(len(manifests_of_kind(self.items, "RoleBinding")), 1)
        self.assertEqual(len(manifests_of_kind(self.items, "NetworkPolicy")), 1)

    def test_serviceaccount_named_after_app(self):
        sa = manifests_of_kind(self.items, "ServiceAccount")[0]
        self.assertEqual(sa["metadata"]["name"], "myapp-sa")
        self.assertEqual(sa["metadata"]["namespace"], "myapp")

    def test_no_optional_resources_when_everything_disabled(self):
        # Disabled database/gitea/messaging and no services must render
        # nothing beyond the five base resources -- no silent extras.
        self.assertEqual(len(manifests_of_kind(self.items, "Cluster")), 0)
        self.assertEqual(len(self.items), 5)


class DatabaseFailClosedTest(unittest.TestCase):
    """spec.database.enabled must fail closed with an actionable Condition
    when the CNPG operator/CRD/webhook prerequisite is not confirmed, and
    must never couple to it silently or create a Cluster prematurely."""

    def test_disabled_renders_nothing_database_related(self):
        items = render({"oxr": make_oxr(database={"enabled": False})})
        self.assertEqual(len(manifests_of_kind(items, "Cluster")), 0)
        self.assertEqual(len(by_kind(items, "RequiredResources")), 0)
        self.assertEqual(len(by_kind(items, "Conditions")), 0)

    def test_enabled_without_cnpg_ready_fails_closed_no_cluster(self):
        items = render({"oxr": make_oxr(database={"enabled": True})})
        self.assertEqual(
            len(manifests_of_kind(items, "Cluster")),
            0,
            "no Cluster may be created before CNPG readiness is confirmed",
        )
        required = by_kind(items, "RequiredResources")
        self.assertEqual(len(required), 1)
        reqs = required[0]["requirements"]
        self.assertEqual(reqs["cnpgCrd"]["kind"], "CustomResourceDefinition")
        self.assertEqual(reqs["cnpgCrd"]["name"], "clusters.postgresql.cnpg.io")
        self.assertEqual(
            reqs["cnpgWebhook"]["kind"], "ValidatingWebhookConfiguration"
        )
        conditions = by_kind(items, "Conditions")
        self.assertEqual(len(conditions), 1)
        cond = conditions[0]["conditions"][0]
        self.assertEqual(cond["condition"]["status"], "False")
        self.assertIn("future-infra", cond["condition"]["message"])
        self.assertEqual(cond["target"], "CompositeAndClaim")

    def test_enabled_with_cnpg_ready_creates_cluster_and_true_condition(self):
        items = render(
            {
                "oxr": make_oxr(appName="dbapp", size="L", database={"enabled": True}),
                "requiredResources": {
                    "cnpgCrd": [{"Resource": {}}],
                    "cnpgWebhook": [{"Resource": {}}],
                },
            }
        )
        clusters = manifests_of_kind(items, "Cluster")
        self.assertEqual(len(clusters), 1)
        cluster = clusters[0]
        self.assertEqual(cluster["metadata"]["name"], "dbapp-db")
        self.assertEqual(cluster["metadata"]["namespace"], "dbapp")
        self.assertEqual(cluster["spec"]["instances"], 2)  # L size -> HA
        self.assertEqual(cluster["spec"]["storage"]["size"], "20Gi")
        cond = by_kind(items, "Conditions")[0]["conditions"][0]
        self.assertEqual(cond["condition"]["status"], "True")

    def test_partial_prerequisite_still_fails_closed(self):
        # CRD present but webhook missing must not be treated as ready.
        items = render(
            {
                "oxr": make_oxr(database={"enabled": True}),
                "requiredResources": {"cnpgCrd": [{"Resource": {}}]},
            }
        )
        self.assertEqual(len(manifests_of_kind(items, "Cluster")), 0)
        cond = by_kind(items, "Conditions")[0]["conditions"][0]
        self.assertEqual(cond["condition"]["status"], "False")

    def test_no_internal_platform_database_coupling(self):
        # The rendered Cluster must never reference platform-db/legacy
        # PostgreSQL credentials -- it is a wholly separate, app-owned Cluster.
        items = render(
            {
                "oxr": make_oxr(database={"enabled": True}),
                "requiredResources": {
                    "cnpgCrd": [{"Resource": {}}],
                    "cnpgWebhook": [{"Resource": {}}],
                },
            }
        )
        cluster = manifests_of_kind(items, "Cluster")[0]
        rendered = yaml.safe_dump(cluster)
        for forbidden in ("platform-db", "postgresql-secrets", "keycloak", "gitea"):
            self.assertNotIn(forbidden, rendered)


class MultiServiceIterationTest(unittest.TestCase):
    """spec.services[] must render every entry, not just index 0."""

    @classmethod
    def setUpClass(cls):
        cls.services_in = [
            {"name": "api", "image": "registry.example.com/myapp/api:1", "port": 8080},
            {"name": "worker", "image": "registry.example.com/myapp/worker:1", "port": 9090},
            {"name": "cron", "image": "registry.example.com/myapp/cron:1", "port": 7070},
        ]
        cls.items = render({"oxr": make_oxr(appName="multiapp", services=cls.services_in)})

    def test_all_three_services_render_deployments(self):
        deployments = manifests_of_kind(self.items, "Deployment")
        names = sorted(d["metadata"]["name"] for d in deployments)
        self.assertEqual(names, ["api", "cron", "worker"])

    def test_all_three_services_render_service_and_ingress(self):
        services = manifests_of_kind(self.items, "Service")
        ingresses = manifests_of_kind(self.items, "Ingress")
        self.assertEqual(
            sorted(s["metadata"]["name"] for s in services),
            ["api-svc", "cron-svc", "worker-svc"],
        )
        self.assertEqual(
            sorted(i["metadata"]["name"] for i in ingresses),
            ["api-ingress", "cron-ingress", "worker-ingress"],
        )

    def test_each_deployment_uses_its_own_image_and_port(self):
        deployments = {
            d["metadata"]["name"]: d for d in manifests_of_kind(self.items, "Deployment")
        }
        for svc in self.services_in:
            container = deployments[svc["name"]]["spec"]["template"]["spec"]["containers"][0]
            self.assertEqual(container["image"], svc["image"])
            self.assertEqual(container["ports"][0]["containerPort"], svc["port"])

    def test_no_index_zero_truncation(self):
        # A regression to index-0-only truncation would collapse this to 1.
        self.assertEqual(len(manifests_of_kind(self.items, "Deployment")), 3)


class MultiSubjectMessagingTest(unittest.TestCase):
    """spec.messaging.subjects[] must render a managed JetStream Stream +
    Consumer for every subject (architecture decision #4), not index 0 only."""

    @classmethod
    def setUpClass(cls):
        cls.subjects = ["myapp.events.>", "myapp.commands", "myapp.audit.v1"]
        cls.items = render(
            {
                "oxr": make_oxr(
                    appName="msgapp",
                    messaging={"enabled": True, "subjects": cls.subjects},
                )
            }
        )

    def test_disabled_renders_no_messaging_resources(self):
        items = render({"oxr": make_oxr(messaging={"enabled": False, "subjects": ["x.y"]})})
        self.assertEqual(len(manifests_of_kind(items, "Stream")), 0)
        self.assertEqual(len(manifests_of_kind(items, "Consumer")), 0)

    def test_enabled_but_empty_subjects_renders_nothing(self):
        items = render({"oxr": make_oxr(messaging={"enabled": True, "subjects": []})})
        self.assertEqual(len(manifests_of_kind(items, "Stream")), 0)

    def test_every_subject_gets_a_stream_and_consumer(self):
        streams = manifests_of_kind(self.items, "Stream")
        consumers = manifests_of_kind(self.items, "Consumer")
        self.assertEqual(len(streams), 3, "no index-0 truncation across subjects")
        self.assertEqual(len(consumers), 3)
        rendered_subjects = sorted(s["spec"]["subjects"][0] for s in streams)
        self.assertEqual(rendered_subjects, sorted(self.subjects))

    def test_stream_names_are_valid_nack_identifiers(self):
        # jetstream.nats.io Stream.spec.name must match ^[^.*>]*$ (no dots,
        # asterisks, or '>' -- NATS subject wildcard characters).
        streams = manifests_of_kind(self.items, "Stream")
        for s in streams:
            self.assertNotRegex(s["spec"]["name"], r"[.*>]")

    def test_consumer_references_matching_stream(self):
        streams = {s["spec"]["name"] for s in manifests_of_kind(self.items, "Stream")}
        for c in manifests_of_kind(self.items, "Consumer"):
            self.assertIn(c["spec"]["streamName"], streams)


class MessagingSubjectCollisionSafetyTest(unittest.TestCase):
    """`streamNameFor` previously derived a Stream's Kubernetes object name
    (and its `spec.name`/`spec.streamName` NATS identifiers) purely from
    `sanitizeSubject(subject)` (`regex.replace(s, "[^a-z0-9]+", "-")`).
    Because that regex only preserves lowercase `[a-z0-9]`, two distinct,
    XRD-accepted subjects that differ only by case or by using `.` vs `-` as
    a separator (e.g. `myapp.events` and `myapp-events`) sanitize to the
    *same* string -- colliding onto the same Stream object name and NATS
    stream name, silently dropping one subject's Stream/Consumer. The fix
    must key the Stream/Consumer identifiers off each subject's *index*
    (already unique, and already used for the `krm.kcl.dev/composition-
    resource-name` annotation), while still passing each subject's exact,
    unsanitized text into `spec.subjects`/`spec.filterSubject`."""

    @classmethod
    def setUpClass(cls):
        # "myapp.events" and "myapp-events" both sanitize (lowercase-only,
        # non-alnum collapsed to '-') to the identical string "myapp-events".
        cls.subjects = ["myapp.events", "myapp-events", "MYAPP.EVENTS"]
        cls.items = render(
            {
                "oxr": make_oxr(
                    appName="collide",
                    messaging={"enabled": True, "subjects": cls.subjects},
                )
            }
        )

    def test_every_colliding_subject_still_gets_its_own_stream_and_consumer(self):
        streams = manifests_of_kind(self.items, "Stream")
        consumers = manifests_of_kind(self.items, "Consumer")
        self.assertEqual(len(streams), 3)
        self.assertEqual(len(consumers), 3)

    def test_stream_object_names_are_unique(self):
        names = [s["metadata"]["name"] for s in manifests_of_kind(self.items, "Stream")]
        self.assertEqual(len(names), len(set(names)), "colliding subjects must not collide onto one Stream name")

    def test_nats_stream_names_are_unique(self):
        names = [s["spec"]["name"] for s in manifests_of_kind(self.items, "Stream")]
        self.assertEqual(len(names), len(set(names)))

    def test_each_stream_preserves_its_own_original_subject(self):
        rendered_subjects = sorted(s["spec"]["subjects"][0] for s in manifests_of_kind(self.items, "Stream"))
        self.assertEqual(rendered_subjects, sorted(self.subjects))

    def test_consumer_references_the_matching_unique_stream(self):
        streams = {s["spec"]["name"] for s in manifests_of_kind(self.items, "Stream")}
        consumers = manifests_of_kind(self.items, "Consumer")
        self.assertEqual(len({c["spec"]["streamName"] for c in consumers}), 3)
        for c in consumers:
            self.assertIn(c["spec"]["streamName"], streams)


class GiteaCapabilityTest(unittest.TestCase):
    """Gitea source repo (with requested visibility) and CI/CD are both
    independently conditional (architecture decisions #2 and #5)."""

    def _gitea_repo_request(self, items):
        matches = [
            i
            for i in by_kind(items, "Request")
            if i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"]
            == "gitea-repo"
        ]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_disabled_renders_no_gitea_or_harbor_resources(self):
        items = render({"oxr": make_oxr(gitea={"enabled": False, "visibility": "private", "cicd": True})})
        self.assertEqual(len(by_kind(items, "Request")), 0)

    def test_enabled_private_no_cicd(self):
        items = render(
            {"oxr": make_oxr(appName="privapp", gitea={"enabled": True, "visibility": "private", "cicd": False})}
        )
        repo_req = self._gitea_repo_request(items)
        body = json.loads(repo_req["spec"]["forProvider"]["payload"]["body"])
        self.assertTrue(body["private"])
        self.assertEqual(body["name"], "privapp")
        # cicd:false must render no CI workflow and no Harbor project/robot.
        names = {
            i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"]
            for i in by_kind(items, "Request")
        }
        self.assertNotIn("gitea-cicd", names)
        self.assertNotIn("harbor-project", names)
        self.assertNotIn("harbor-robot", names)

    def test_enabled_public_with_cicd(self):
        items = render(
            {"oxr": make_oxr(appName="pubapp", gitea={"enabled": True, "visibility": "public", "cicd": True})}
        )
        repo_req = self._gitea_repo_request(items)
        body = json.loads(repo_req["spec"]["forProvider"]["payload"]["body"])
        self.assertFalse(body["private"])
        names = {
            i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"]
            for i in by_kind(items, "Request")
        }
        self.assertIn("gitea-cicd", names)
        self.assertIn("harbor-project", names)
        self.assertIn("harbor-robot", names)

    def test_gitea_repo_request_has_observe_mapping_for_idempotency(self):
        items = render(
            {"oxr": make_oxr(appName="idemapp", gitea={"enabled": True, "visibility": "private", "cicd": False})}
        )
        repo_req = self._gitea_repo_request(items)
        methods = {m["method"] for m in repo_req["spec"]["forProvider"]["mappings"]}
        self.assertEqual(
            methods,
            {"POST", "GET", "PATCH"},
            "must observe-before-create for idempotent resume, plus PATCH to repair visibility drift",
        )

    def test_harbor_project_uses_head_existence_check_not_list(self):
        items = render(
            {"oxr": make_oxr(appName="hbapp", gitea={"enabled": True, "visibility": "public", "cicd": True})}
        )
        harbor_project = next(
            i
            for i in by_kind(items, "Request")
            if i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"]
            == "harbor-project"
        )
        methods = {m["method"] for m in harbor_project["spec"]["forProvider"]["mappings"]}
        self.assertIn("HEAD", methods)


class HarborRobotSecretInjectionTest(unittest.TestCase):
    """The Harbor robot secret must be captured server-side into a Secret via
    secretInjectionConfigs -- never embedded in the Composition/manifest/Git."""

    def test_robot_response_captured_via_secret_injection_not_literal(self):
        items = render(
            {"oxr": make_oxr(appName="secapp", gitea={"enabled": True, "visibility": "public", "cicd": True})}
        )
        robot_req = next(
            i
            for i in by_kind(items, "Request")
            if i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"]
            == "harbor-robot"
        )
        injections = robot_req["spec"]["forProvider"]["secretInjectionConfigs"]
        self.assertEqual(len(injections), 1)
        self.assertEqual(injections[0]["secretRef"]["namespace"], "secapp")
        keys = {m["secretKey"] for m in injections[0]["keyMappings"]}
        self.assertEqual(keys, {"name", "secret"})
        # The robot secret must never appear as a literal in the request body.
        body = robot_req["spec"]["forProvider"]["payload"]["body"]
        self.assertNotIn("secret\":", body.replace(" ", ""))


class TransportSecurityTest(unittest.TestCase):
    """Every credential-bearing Gitea/Harbor API call this Composition renders
    must go through the trusted digiorg.local ingress over HTTPS (CA:
    cert-manager/digiorg-local-ca-secret), never a raw in-cluster plain-HTTP
    Service address -- and the generated CI workflow must trust that same CA
    for git/Node operations inside the ephemeral job containers the Gitea
    Actions runner spawns (core/platform/base/gitea-actions-runner)."""

    def _request_baseurls(self, items):
        return [
            i["spec"]["forProvider"]["payload"]["baseUrl"]
            for i in by_kind(items, "Request")
        ]

    def test_every_request_base_url_is_https_digiorg_local(self):
        items = render(
            {"oxr": make_oxr(appName="tlsapp", gitea={"enabled": True, "visibility": "public", "cicd": True})}
        )
        base_urls = self._request_baseurls(items)
        self.assertGreater(len(base_urls), 0)
        for base_url in base_urls:
            self.assertTrue(
                base_url.startswith("https://digiorg.local/"),
                f"expected an https://digiorg.local/... base URL, got {base_url!r}",
            )

    def test_no_request_uses_plain_http_or_in_cluster_service_dns(self):
        items = render(
            {"oxr": make_oxr(appName="tlsapp2", gitea={"enabled": True, "visibility": "public", "cicd": True})}
        )
        for base_url in self._request_baseurls(items):
            self.assertNotIn("http://", base_url)
            self.assertNotIn(".svc.cluster.local", base_url)

    def test_docker_registry_tag_uses_the_trusted_ingress_host(self):
        items = render(
            {
                "oxr": make_oxr(
                    appName="tlsbuild",
                    services=[{"name": "api", "image": "unused", "port": 8080, "build": {"enabled": True}}],
                    gitea={"enabled": True, "visibility": "private", "cicd": True},
                )
            }
        )
        _, text = _ci_workflow_yaml(items)
        self.assertIn("digiorg.local/tlsbuild/api", text)
        self.assertNotIn(".svc.cluster.local", text)

    def test_generated_ci_workflow_trusts_the_ca_for_git_and_node(self):
        items = render(
            {
                "oxr": make_oxr(
                    appName="tlsca",
                    services=[{"name": "api", "image": "unused", "port": 8080, "build": {"enabled": True}}],
                    gitea={"enabled": True, "visibility": "private", "cicd": True},
                )
            }
        )
        workflow, _ = _ci_workflow_yaml(items)
        env = workflow.get("env", {})
        self.assertIn("GIT_SSL_CAINFO", env)
        self.assertIn("NODE_EXTRA_CA_CERTS", env)
        self.assertEqual(env["GIT_SSL_CAINFO"], env["NODE_EXTRA_CA_CERTS"])
        self.assertTrue(env["GIT_SSL_CAINFO"].startswith("/"))


class HarborRobotSecretsPushedToGiteaActionsTest(unittest.TestCase):
    """Issue #285 blocker #6: the per-app Harbor robot Secret
    (${appName}-harbor-robot) must reach the Gitea repository's Actions
    secrets HARBOR_ROBOT_NAME and HARBOR_ROBOT_SECRET via the exact Gitea
    1.23 API (PUT /repos/{owner}/{repo}/actions/secrets/{secretname}, body
    {"data": "<value>"}), so the generated CI workflow's `secrets.
    HARBOR_ROBOT_NAME`/`secrets.HARBOR_ROBOT_SECRET` references actually
    resolve. Never a literal value -- sourced only from the Secret the
    harbor-robot Request itself populates."""

    @classmethod
    def setUpClass(cls):
        cls.items = render(
            {"oxr": make_oxr(appName="secpush", gitea={"enabled": True, "visibility": "public", "cicd": True})}
        )
        cls.requests_by_slug = {
            i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"]: i
            for i in by_kind(cls.items, "Request")
        }

    def test_both_secret_name_requests_are_present(self):
        self.assertIn("gitea-secret-harbor-robot-name", self.requests_by_slug)
        self.assertIn("gitea-secret-harbor-robot-secret", self.requests_by_slug)

    def _create_mapping(self, req):
        return next(
            m for m in req["spec"]["forProvider"]["mappings"] if m.get("action") == "CREATE"
        )

    def _observe_mapping(self, req):
        return next(
            m for m in req["spec"]["forProvider"]["mappings"] if m.get("action") == "OBSERVE"
        )

    def test_exact_endpoint_and_method_for_each_secret_name(self):
        for slug, secret_name in (
            ("gitea-secret-harbor-robot-name", "HARBOR_ROBOT_NAME"),
            ("gitea-secret-harbor-robot-secret", "HARBOR_ROBOT_SECRET"),
        ):
            with self.subTest(slug=slug):
                req = self.requests_by_slug[slug]
                create = self._create_mapping(req)
                self.assertEqual(create["method"], "PUT")
                self.assertIn(
                    "/repos/DigiOrg/secpush/actions/secrets/%s" % secret_name, create["url"]
                )
                observe = self._observe_mapping(req)
                self.assertEqual(observe["method"], "GET")
                self.assertIn("/repos/DigiOrg/secpush/actions/secrets", observe["url"])
                self.assertNotIn("secrets/%s" % secret_name, observe["url"])

    def test_body_schema_is_exactly_data_and_description_fields_and_data_is_never_a_literal(self):
        # Issue #285 review finding (rotated Harbor CI robot credential
        # propagation): `description` carries an observable, non-secret
        # Harbor-robot-version marker (see
        # tests/test_harbor_credential_rotation_propagation.py) so a rotated
        # robot's now-stale Gitea secret can be detected -- `data` remains
        # the only field ever sourced from a credential placeholder.
        for slug, source_key in (
            ("gitea-secret-harbor-robot-name", "name"),
            ("gitea-secret-harbor-robot-secret", "secret"),
        ):
            with self.subTest(slug=slug):
                req = self.requests_by_slug[slug]
                body = json.loads(req["spec"]["forProvider"]["payload"]["body"])
                self.assertEqual(set(body.keys()), {"data", "description"})
                self.assertRegex(
                    body["data"],
                    r"^\{\{\s*[^:{}\s]+:[^:{}\s]+:%s\s*\}\}$" % re.escape(source_key),
                )
                self.assertIn("secpush-harbor-robot", body["data"])
                self.assertIn(":secpush:", body["data"])
                self.assertNotIn("{{", body["description"])
                self.assertNotIn("}}", body["description"])

    def test_secret_placeholder_references_the_per_app_namespace_not_a_shared_one(self):
        for slug in ("gitea-secret-harbor-robot-name", "gitea-secret-harbor-robot-secret"):
            req = self.requests_by_slug[slug]
            body = json.loads(req["spec"]["forProvider"]["payload"]["body"])
            self.assertNotIn("crossplane-system", body["data"])

    def test_authorization_uses_the_least_privilege_gitea_credential(self):
        for slug in ("gitea-secret-harbor-robot-name", "gitea-secret-harbor-robot-secret"):
            req = self.requests_by_slug[slug]
            auth = req["spec"]["forProvider"]["headers"]["Authorization"][0]
            self.assertIn("crossplane-gitea-credentials:crossplane-system:token", auth)


class NoLiteralCredentialsTest(unittest.TestCase):
    """Static scan of the Composition source: only {{ name:namespace:key }}
    provider-http secret placeholders may appear in any Authorization header
    or request body -- never a literal Bearer/Basic token or 'changeme'."""

    def test_no_changeme_placeholder_anywhere(self):
        source = load_pipeline_source()
        self.assertNotIn("changeme", source.lower())

    def test_authorization_headers_are_secret_placeholders_only(self):
        source = load_pipeline_source()
        for match in re.finditer(r'Authorization"?\s*=\s*\[\s*"([^"]+)"', source):
            value = match.group(1)
            self.assertRegex(
                value,
                r"\{\{\s*[^:{}\s]+:[^:{}\s]+:[^:{}\s]+\s*\}\}",
                "Authorization header %r is not a provider-http secret placeholder" % value,
            )

    def test_full_render_contains_no_literal_secret_values(self):
        # Belt-and-braces: render the richest scenario and confirm the only
        # "Authorization"-shaped strings in the output are placeholders.
        items = render(
            {
                "oxr": make_oxr(
                    appName="credcheck",
                    database={"enabled": True},
                    gitea={"enabled": True, "visibility": "public", "cicd": True},
                ),
                "requiredResources": {
                    "cnpgCrd": [{"Resource": {}}],
                    "cnpgWebhook": [{"Resource": {}}],
                },
            }
        )
        found_any = False
        for item in items:
            headers = (
                item.get("spec", {}).get("forProvider", {}).get("headers", {})
            )
            for value in headers.get("Authorization", []):
                found_any = True
                self.assertIn(
                    "{{", value, "Authorization header %r is not a placeholder" % value
                )
        self.assertTrue(found_any, "expected at least one Authorization header in this scenario")


def _gitea_cicd_request(items):
    return next(
        i
        for i in by_kind(items, "Request")
        if i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"] == "gitea-cicd"
    )


def _ci_workflow_yaml(items):
    """Decode the generated Gitea Actions CI workflow file from the
    gitea-cicd Request's PUT-contents body, and parse it as real YAML (not a
    string-contains guess) so job/step structure is asserted precisely."""
    req = _gitea_cicd_request(items)
    body = json.loads(req["spec"]["forProvider"]["payload"]["body"])
    text = base64.b64decode(body["content"]).decode("utf-8")
    return yaml.safe_load(text), text


# Verified upstream 2026-07-22 via GitHub API:
# https://api.github.com/repos/actions/checkout/git/refs/tags/v4.2.2
PINNED_CHECKOUT_SHA = "11bd71901bbe5b1630ceea73d27597364c9af683"


class CheckoutActionPinTest(unittest.TestCase):
    """actions/checkout must be pinned to an immutable commit SHA -- a
    floating major-version tag like @v4 is mutable upstream and defeats
    supply-chain pinning (Issue #285 blocker #4)."""

    def test_source_never_references_the_floating_v4_tag(self):
        source = load_pipeline_source()
        self.assertNotIn("actions/checkout@v4", source)
        self.assertNotIn("actions/checkout@v", source)

    def test_source_uses_the_pinned_commit_sha(self):
        source = load_pipeline_source()
        self.assertIn("actions/checkout@%s" % PINNED_CHECKOUT_SHA, source)

    def test_generated_ci_workflow_uses_the_pinned_sha(self):
        items = render(
            {
                "oxr": make_oxr(
                    appName="pinapp",
                    gitea={"enabled": True, "visibility": "private", "cicd": True},
                    services=[
                        {"name": "api", "image": "unused", "port": 8080, "build": {"enabled": True}}
                    ],
                )
            }
        )
        _, text = _ci_workflow_yaml(items)
        self.assertIn(PINNED_CHECKOUT_SHA, text)


class MultiServiceBuildContractTest(unittest.TestCase):
    """Every service that opts into build.enabled gets its own CI job with a
    deterministic Harbor repo/tag; services that don't opt in are never
    referenced by the generated workflow (they are assumed to reference an
    already-published, externally-built image). When gitea.cicd is on but no
    service opts in, the workflow must be honest that there is nothing to
    build -- not invent a single fake ${appName}/${appName} image the way
    the old broken implementation did (Issue #285 blocker #5)."""

    def test_each_buildable_service_gets_its_own_job_and_harbor_tag(self):
        services = [
            {
                "name": "api",
                "image": "unused",
                "port": 8080,
                "build": {"enabled": True, "context": "services/api"},
            },
            {"name": "worker", "image": "unused", "port": 9090, "build": {"enabled": True}},
            {"name": "cron", "image": "unused", "port": 7070},  # not buildable
        ]
        items = render(
            {
                "oxr": make_oxr(
                    appName="buildapp",
                    services=services,
                    gitea={"enabled": True, "visibility": "private", "cicd": True},
                )
            }
        )
        workflow, text = _ci_workflow_yaml(items)
        self.assertEqual(set(workflow["jobs"]), {"build-api", "build-worker"})
        self.assertIn(
            "digiorg.local/buildapp/api':${{ gitea.sha }}", text
        )
        self.assertIn(
            "digiorg.local/buildapp/worker':${{ gitea.sha }}", text
        )
        self.assertIn("services/api", text)
        self.assertNotIn("buildapp/cron", text, "non-buildable service must never be referenced")

    def test_no_buildable_services_renders_an_honest_placeholder_not_a_fake_image(self):
        items = render(
            {
                "oxr": make_oxr(
                    appName="nobuild", gitea={"enabled": True, "visibility": "private", "cicd": True}
                )
            }
        )
        workflow, text = _ci_workflow_yaml(items)
        self.assertNotIn("nobuild/nobuild", text)
        self.assertEqual(set(workflow["jobs"]), {"no-buildable-services"})

    def test_build_context_defaults_to_repository_root(self):
        items = render(
            {
                "oxr": make_oxr(
                    appName="ctxdefault",
                    services=[{"name": "api", "image": "unused", "port": 8080, "build": {"enabled": True}}],
                    gitea={"enabled": True, "visibility": "private", "cicd": True},
                )
            }
        )
        _, text = _ci_workflow_yaml(items)
        self.assertIn(
            "docker build -t 'digiorg.local/ctxdefault/api':${{ gitea.sha }} -- '.'",
            text,
        )

    def test_generated_workflow_is_syntactically_valid_yaml_with_jobs_mapping(self):
        # Belt-and-braces on the richest scenario: three buildable services.
        services = [
            {"name": s, "image": "unused", "port": 8080, "build": {"enabled": True}}
            for s in ("a", "b", "c")
        ]
        items = render(
            {
                "oxr": make_oxr(
                    appName="richci",
                    services=services,
                    gitea={"enabled": True, "visibility": "private", "cicd": True},
                )
            }
        )
        workflow, _ = _ci_workflow_yaml(items)
        self.assertEqual(workflow["name"], "CI")
        self.assertEqual(len(workflow["jobs"]), 3)
        for job in workflow["jobs"].values():
            self.assertEqual(job["runs-on"], "ubuntu-latest")


class PinnedFunctionsTest(unittest.TestCase):
    """The Composition must reference the two pinned Crossplane Functions and
    no other rendering mechanism (single deterministic pipeline)."""

    def test_pipeline_has_exactly_two_steps(self):
        with open(PIPELINE_COMPOSITION, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        steps = doc["spec"]["pipeline"]
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["functionRef"]["name"], "function-kcl")
        self.assertEqual(steps[1]["functionRef"]["name"], "function-auto-ready")

    def test_only_one_composition_targets_the_application_xr_locally(self):
        local_dir = os.path.join(os.path.dirname(PIPELINE_COMPOSITION))
        composition_files = [
            f
            for f in os.listdir(local_dir)
            if f.endswith(".yaml") and f != "kustomization.yaml"
        ]
        self.assertEqual(
            composition_files,
            ["pipeline.yaml"],
            "exactly one Composition must exist -- the five competing "
            "Compositions this issue replaces must be gone",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
