#!/usr/bin/env python3
"""Issue #285 blocker (CI shell injection): `compositions/local/pipeline.yaml`
`buildJobFor` previously spliced `spec.services[].name` (via the Harbor
`${tag}`) and `spec.services[].build.context` *unquoted* into a generated
Gitea Actions `run:` shell line:

    run = "docker build -t ${tag}:${{ gitea.sha }} ${ctx}"

The companion fix in `core`'s Application XRD
(`crossplane/xrds/application.yaml`) now rejects dangerous values for both
fields at admission time (see
`core/platform/tests/test_appclaim_service_field_hardening.py`). This test
proves the second, defense-in-depth layer that lives here in the
Composition itself: *even if* an adversarial value reached this render (a
future schema relaxation, a different Claim path, a bug in the XRD), the
generated shell command can no longer let it break out of its intended
single-argument position, because every KCL-interpolated value is now
double-quoted and the build path is additionally isolated behind `--`.

The proof is not a string match -- it actually executes the rendered `run:`
line with a real `bash` (after substituting the two GitHub/Gitea-Actions
`${{ }}` expressions the same way the Actions runner would, since those are
runtime-substituted before the shell ever sees the script; `docker` itself
is stubbed to a no-op since it is not installed in this sandbox) and checks
whether an injected side-effect command actually ran.

Run:
    python3 -m unittest tests.test_ci_shell_injection_hardening -v
"""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from render_harness import by_kind, make_oxr, ready_cicd_context, render  # noqa: E402


def _ci_workflow_text(services, **oxr_kwargs):
    app_name = oxr_kwargs.pop("appName", "shellinj")
    ready = ready_cicd_context(app_name)
    buildable_count = sum(
        1 for svc in services if svc.get("build", {}).get("enabled", False)
    )
    for i in range(buildable_count):
        ready["ocds"][f"source-scaffold-{i}"] = {
            "Resource": {
                "status": {
                    "response": {
                        "statusCode": 200,
                        "body": json.dumps({"type": "file", "content": "dXNlciBzb3VyY2U="}),
                    }
                }
            }
        }
    items = render(
        {
            "oxr": make_oxr(
                appName=app_name,
                services=services,
                gitea={"enabled": True, "visibility": "private", "cicd": True},
                **oxr_kwargs,
            ),
            **ready,
        }
    )
    req = next(
        i
        for i in by_kind(items, "Request")
        if i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"] == "gitea-cicd"
    )
    body = json.loads(req["spec"]["forProvider"]["payload"]["body"])
    return base64.b64decode(body["content"]).decode("utf-8")


def _run_step(workflow_text, job_key, step_name):
    import yaml

    workflow = yaml.safe_load(workflow_text)
    job = workflow["jobs"][job_key]
    step = next(s for s in job["steps"] if s.get("name") == step_name)
    return step["run"]


def _simulate_actions_expression_substitution(run_line):
    """Gitea/GitHub Actions substitutes `${{ ... }}` expressions with literal
    values before ever handing the script to a shell -- reproduce exactly
    that (and nothing more) so the shell only ever sees what a real runner
    would feed it."""
    substituted = run_line.replace("${{ gitea.sha }}", "deadbeef1234")
    substituted = re.sub(r"\$\{\{\s*secrets\.[A-Z_]+\s*\}\}", "dummy-secret-value", substituted)
    return substituted


def _executes_injected_side_effect(run_line, marker_path):
    """Execute the rendered `run:` line with real bash (docker stubbed to a
    no-op) and report whether a payload embedded in `run_line` that
    references $MARKER_FILE managed to create it -- i.e. whether the
    injected shell text escaped its intended single-argument position."""
    simulated = _simulate_actions_expression_substitution(run_line)
    script = "docker() { :; }\n%s\n" % simulated
    env = dict(os.environ)
    env["MARKER_FILE"] = marker_path
    subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    return os.path.exists(marker_path)


INJECTION_PAYLOADS = [
    '. ; touch "$MARKER_FILE"',
    '. && touch "$MARKER_FILE"',
    '. | tee "$MARKER_FILE" >/dev/null',
    '$(touch "$MARKER_FILE")',
    '`touch "$MARKER_FILE"`',
    '.; touch "$MARKER_FILE" #',
]

NAME_INJECTION_PAYLOADS = [
    'api" ; touch "$MARKER_FILE" ; echo "',
    "api' ; touch \"$MARKER_FILE\" ; echo '",
    "api;touch \"$MARKER_FILE\";echo x",
]


class BuildContextCannotBecomeShellSyntaxTest(unittest.TestCase):
    """Even an adversarial build.context that reached the renderer must stay
    inert as a single quoted argument to `docker build`."""

    def test_injected_build_context_does_not_execute(self):
        for payload in INJECTION_PAYLOADS:
            with self.subTest(payload=payload):
                text = _ci_workflow_text(
                    [{"name": "api", "image": "unused", "port": 8080, "build": {"enabled": True, "context": payload}}]
                )
                run_line = _run_step(text, "build-api", "Build immutable image")
                with tempfile.TemporaryDirectory() as tmp:
                    marker = os.path.join(tmp, "pwned")
                    executed = _executes_injected_side_effect(run_line, marker)
                self.assertFalse(
                    executed,
                    "build.context payload %r escaped its quoted argument and executed: %r"
                    % (payload, run_line),
                )


class ServiceNameCannotBecomeShellSyntaxTest(unittest.TestCase):
    """Even an adversarial service name (which flows into the Harbor tag)
    that reached the renderer must stay inert as a single quoted argument."""

    def test_injected_service_name_does_not_execute(self):
        for payload in NAME_INJECTION_PAYLOADS:
            with self.subTest(payload=payload):
                ready = ready_cicd_context("shellinj")
                items = render(
                    {
                        "oxr": make_oxr(
                            appName="shellinj",
                            services=[{"name": payload, "image": "unused", "port": 8080, "build": {"enabled": True}}],
                            gitea={"enabled": True, "visibility": "private", "cicd": True},
                        ),
                        **ready,
                    }
                )
                req = next(
                    i
                    for i in by_kind(items, "Request")
                    if i["metadata"]["annotations"]["krm.kcl.dev/composition-resource-name"] == "gitea-cicd"
                )
                body = json.loads(req["spec"]["forProvider"]["payload"]["body"])
                text = base64.b64decode(body["content"]).decode("utf-8")
                import yaml

                workflow = yaml.safe_load(text)
                job_key = next(k for k in workflow["jobs"] if k.startswith("build-"))
                job = workflow["jobs"][job_key]
                run_line = next(s for s in job["steps"] if s.get("name") == "Build immutable image")["run"]
                with tempfile.TemporaryDirectory() as tmp:
                    marker = os.path.join(tmp, "pwned")
                    executed = _executes_injected_side_effect(run_line, marker)
                self.assertFalse(
                    executed,
                    "service.name payload %r escaped its quoted argument and executed: %r"
                    % (payload, run_line),
                )


class HarborLoginUsesPrintfNotEchoTest(unittest.TestCase):
    """The Harbor password must go to docker login's stdin via `printf`, not
    `echo` (some `echo` implementations interpret backslash escapes in the
    secret value; `printf '%s'` never does)."""

    def test_login_step_uses_printf(self):
        text = _ci_workflow_text([{"name": "api", "image": "unused", "port": 8080, "build": {"enabled": True}}])
        run_line = _run_step(text, "build-api", "Log in to Harbor")
        self.assertIn("printf '%s'", run_line)
        self.assertNotRegex(run_line, r"^echo\b")


class HarborLoginSecretsCannotBecomeShellSyntaxTest(unittest.TestCase):
    """Actions secrets are data, not shell source.  In particular Harbor
    robot names contain ``$`` and must reach docker byte-for-byte."""

    def test_login_secrets_are_injected_via_env_and_preserved_byte_exactly(self):
        import yaml

        text = _ci_workflow_text(
            [{"name": "api", "image": "unused", "port": 8080, "build": {"enabled": True}}]
        )
        workflow = yaml.safe_load(text)
        step = next(
            s for s in workflow["jobs"]["build-api"]["steps"]
            if s.get("name") == "Log in to Harbor"
        )
        self.assertEqual(
            step.get("env"),
            {
                "HARBOR_ROBOT_NAME": "${{ secrets.HARBOR_ROBOT_NAME }}",
                "HARBOR_ROBOT_SECRET": "${{ secrets.HARBOR_ROBOT_SECRET }}",
            },
        )
        self.assertNotIn("${{ secrets.", step["run"])

        username = "robot$crossplane-system+app-ci"
        password = 'pa$$`touch "$MARKER_FILE"`$(touch "$MARKER_FILE")'
        with tempfile.TemporaryDirectory() as tmp:
            username_file = os.path.join(tmp, "username")
            password_file = os.path.join(tmp, "password")
            marker = os.path.join(tmp, "unsafe")
            script = (
                "docker() { "
                "test \"$1\" = login; shift; "
                "while test $# -gt 0; do "
                "if test \"$1\" = -u; then printf '%s' \"$2\" >\"$USERNAME_FILE\"; shift 2; "
                "elif test \"$1\" = --password-stdin; then cat >\"$PASSWORD_FILE\"; shift; "
                "else shift; fi; done; }\n"
                + step["run"]
            )
            env = dict(os.environ)
            env.update(
                HARBOR_ROBOT_NAME=username,
                HARBOR_ROBOT_SECRET=password,
                USERNAME_FILE=username_file,
                PASSWORD_FILE=password_file,
                MARKER_FILE=marker,
            )
            proc = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True,
                timeout=10, env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(username_file, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), username)
            with open(password_file, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), password)
            self.assertFalse(os.path.exists(marker))


class QuotingAndDoubleDashPresentTest(unittest.TestCase):
    """Static assertions on the exact rendered shell text, so a regression
    that removes quoting is caught even if it doesn't happen to be
    exploitable with today's payload list."""

    def test_build_step_quotes_tag_and_context_and_uses_double_dash(self):
        text = _ci_workflow_text(
            [{"name": "api", "image": "unused", "port": 8080, "build": {"enabled": True, "context": "services/api"}}]
        )
        run_line = _run_step(text, "build-api", "Build immutable image")
        self.assertEqual(
            run_line,
            "docker build -t 'digiorg.local/shellinj/api':${{ gitea.sha }} -- 'services/api'",
        )

    def test_push_step_quotes_tag(self):
        text = _ci_workflow_text([{"name": "api", "image": "unused", "port": 8080, "build": {"enabled": True}}])
        run_line = _run_step(text, "build-api", "Push immutable image")
        self.assertEqual(
            run_line,
            "docker push 'digiorg.local/shellinj/api':${{ gitea.sha }}",
        )

    def test_login_step_quotes_registry_and_environment_username(self):
        text = _ci_workflow_text([{"name": "api", "image": "unused", "port": 8080, "build": {"enabled": True}}])
        run_line = _run_step(text, "build-api", "Log in to Harbor")
        self.assertIn("docker login 'digiorg.local'", run_line)
        self.assertIn('-u "$HARBOR_ROBOT_NAME"', run_line)
        self.assertNotIn("${{ secrets.", run_line)


class NormalMultiServiceRenderStillGreenTest(unittest.TestCase):
    """The hardening must not change behavior for ordinary, safe input --
    same jobs, same tags, same context, just quoted."""

    def test_three_buildable_services_all_render_correctly(self):
        services = [
            {"name": "api", "image": "unused", "port": 8080, "build": {"enabled": True, "context": "services/api"}},
            {"name": "worker", "image": "unused", "port": 9090, "build": {"enabled": True}},
            {"name": "cron", "image": "unused", "port": 7070},
        ]
        text = _ci_workflow_text(services, appName="multibuild")
        import yaml

        workflow = yaml.safe_load(text)
        self.assertEqual(set(workflow["jobs"]), {"build-api", "build-worker"})
        self.assertIn(
            "docker build -t 'digiorg.local/multibuild/api':${{ gitea.sha }} -- 'services/api'",
            text,
        )
        self.assertIn(
            "docker build -t 'digiorg.local/multibuild/worker':${{ gitea.sha }} -- '.'",
            text,
        )
        self.assertNotIn("multibuild/cron", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
