#!/usr/bin/env python3
"""Behavioral validation of the exact generated NGINX scaffold image."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, os.path.dirname(__file__))
from test_fresh_source_scaffold import _dockerfile, _render  # noqa: E402


def _assert_exact_http_metadata(test_case, metadata):
    statuses = re.findall(
        r"(?mi)^[ \t]*HTTP/1\.[01][ \t]+([0-9]{3})[ \t]+([^\r\n]+?)[ \t]*\r?$",
        metadata,
    )
    test_case.assertEqual(statuses, [("200", "OK")])
    content_types = re.findall(
        r"(?mi)^[ \t]*Content-Type:[ \t]*([^\r\n]+?)[ \t]*\r?$",
        metadata,
    )
    test_case.assertEqual(content_types, ["text/plain"])


class ExactHttpMetadataTest(unittest.TestCase):
    def test_accepts_exact_200_text_plain_response(self):
        _assert_exact_http_metadata(
            self,
            "  HTTP/1.1 200 OK\n  Content-Type: text/plain\n",
        )

    def test_rejects_non_200_response(self):
        with self.assertRaises(AssertionError):
            _assert_exact_http_metadata(
                self,
                "  HTTP/1.1 201 Created\n  Content-Type: text/plain\n",
            )

    def test_rejects_non_exact_content_type(self):
        with self.assertRaises(AssertionError):
            _assert_exact_http_metadata(
                self,
                "  HTTP/1.1 200 OK\n  Content-Type: text/plain; charset=utf-8\n",
            )


class GeneratedNginxRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        docker = shutil.which("docker")
        if docker is None:
            raise RuntimeError(
                "docker CLI is required for the authoritative scaffold runtime gate"
            )
        cls.docker = docker
        probe = subprocess.run(
            [cls.docker, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if probe.returncode != 0 or not probe.stdout.strip():
            raise RuntimeError(
                "a reachable Docker daemon is required for the authoritative "
                f"scaffold runtime gate: {probe.stderr.strip()}"
            )

    def _docker(self, *args, timeout=60, check=True):
        proc = subprocess.run(
            [self.docker, *args], capture_output=True, text=True, timeout=timeout
        )
        if check and proc.returncode != 0:
            self.fail(
                "docker %s failed with rc=%s\nstdout=%s\nstderr=%s"
                % (" ".join(args[:3]), proc.returncode, proc.stdout, proc.stderr)
            )
        return proc

    def _assert_generated_image(self, app, service, context, port):
        services = [{
            "name": service,
            "image": "unused",
            "port": port,
            "build": {"enabled": True, "context": context},
        }]
        path = "Dockerfile" if context == "." else f"{context}/Dockerfile"
        dockerfile = _dockerfile(_render(services=services, app=app), path=path)
        tag = f"digiorg-scaffold-test:{uuid.uuid4().hex}"
        container = None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with open(os.path.join(tmp, "Dockerfile"), "w", encoding="utf-8") as fh:
                    fh.write(dockerfile)
                self._docker("build", "--pull=false", "--tag", tag, tmp, timeout=180)

            syntax = self._docker("run", "--rm", tag, "nginx", "-t", timeout=30)
            self.assertIn("syntax is ok", syntax.stderr)
            self.assertIn("test is successful", syntax.stderr)

            container = self._docker("run", "--rm", "--detach", tag, timeout=30).stdout.strip()
            self.assertRegex(container, r"^[0-9a-f]{64}$")
            body = None
            for _attempt in range(20):
                probe = self._docker(
                    "exec", container, "wget", "-qO-", f"http://127.0.0.1:{port}/",
                    timeout=10, check=False,
                )
                if probe.returncode == 0:
                    body = probe.stdout
                    break
                time.sleep(0.25)
            self.assertIsNotNone(body, "NGINX did not become ready within 5 seconds")
            self.assertEqual(body, f"DigiOrg - {app}")

            response = self._docker(
                "exec", container, "wget", "-S", "-O", "-",
                f"http://127.0.0.1:{port}/", timeout=10,
            )
            self.assertEqual(response.stdout, f"DigiOrg - {app}")
            _assert_exact_http_metadata(self, response.stderr)
        finally:
            if container:
                self._docker("rm", "--force", container, timeout=20, check=False)
            self._docker("image", "rm", "--force", tag, timeout=30, check=False)

    def test_root_context_image_has_valid_nginx_and_app_response(self):
        self._assert_generated_image("alpha-app", "web", ".", 9950)

    def test_nested_context_image_supports_maximum_hyphenated_app_name(self):
        app = "a" * 15 + "-" + "b" * 16
        self._assert_generated_image(app, "api", "services/api", 9951)


if __name__ == "__main__":
    unittest.main(verbosity=2)
