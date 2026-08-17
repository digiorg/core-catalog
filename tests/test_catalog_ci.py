#!/usr/bin/env python3
"""Remote CI contract for the core-catalog validation gate."""

import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "catalog-validation.yml"
REQUIREMENTS = ROOT / ".github" / "requirements-catalog-validation-py312-linux.txt"


class CatalogValidationWorkflowTest(unittest.TestCase):
    def workflow(self):
        self.assertTrue(WORKFLOW.is_file(), f"missing validation workflow: {WORKFLOW}")
        return WORKFLOW.read_text(encoding="utf-8")

    def test_runs_for_push_and_pull_requests_with_read_only_contents(self):
        text = self.workflow()
        self.assertRegex(text, r"(?m)^\s+push:\s*$")
        self.assertRegex(text, r"(?m)^\s+pull_request:\s*$")
        self.assertRegex(text, r"(?m)^permissions:\s*\n\s+contents:\s+read\s*$")

    def test_actions_are_sha_pinned_and_python_is_312(self):
        text = self.workflow()
        uses = re.findall(r"(?m)^\s*-?\s*uses:\s*([^\s#]+)", text)
        self.assertGreaterEqual(len(uses), 2)
        for action in uses:
            self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$", action)
        self.assertIn('python-version: "3.12"', text)

    def test_full_suite_and_docker_runtime_gate_are_mandatory(self):
        text = self.workflow()
        self.assertIn("docker version", text)
        self.assertIn(
            "python3 -m unittest discover -s tests -p 'test_*.py' -v", text
        )
        self.assertNotIn("SCAFFOLD_RUNTIME_TEST", text)
        self.assertNotRegex(text, r"(?i)continue-on-error:\s*true")

    def test_python_dependencies_are_version_and_hash_pinned(self):
        text = self.workflow()
        self.assertIn(
            "pip install --disable-pip-version-check --only-binary=:all: "
            "--require-hashes -r "
            ".github/requirements-catalog-validation-py312-linux.txt",
            text,
        )
        self.assertTrue(REQUIREMENTS.is_file(), f"missing requirements: {REQUIREMENTS}")
        requirements = REQUIREMENTS.read_text(encoding="utf-8")
        self.assertRegex(
            requirements,
            r"(?m)^PyYAML==6\.0\.3 --hash=sha256:[0-9a-f]{64}$",
        )

    def test_kustomize_is_checksum_verified_and_rendered(self):
        text = self.workflow()
        self.assertIn("KUSTOMIZE_VERSION=v5.8.1", text)
        self.assertRegex(text, r"[0-9a-f]{64}\s+/tmp/kustomize\.tgz")
        self.assertIn("sha256sum -c -", text)
        self.assertIn("kustomize build compositions/local", text)
        self.assertIn("git diff --check", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
