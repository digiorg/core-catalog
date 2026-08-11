"""Render harness for the DigiOrg AppClaim pipeline Composition.

The platform's single deterministic Composition (`compositions/local/pipeline.yaml`)
uses the pinned `crossplane-contrib/function-kcl` Crossplane Function (see
`core/crossplane/providers/packages/function-kcl.yaml`, pinned at v0.12.2). That
function evaluates the KCL source embedded in the Composition's pipeline step
against a `params` document containing the observed composite resource (`oxr`), any
requested/required resources (`requiredResources`) and the pipeline context (`ctx`),
and returns a list of desired composed resources (`items`).

There is no `crossplane` CLI or Docker runtime available in CI/dev sandboxes, so this
harness renders the *actual* KCL source (single source of truth, extracted directly
out of the Composition YAML -- never duplicated) with the real KCL language runtime
rather than a hand-rolled simulation of KCL semantics.

Reproducibility: rather than relying on an undocumented globally-installed `kcl`/
`kclvm_cli` binary, this module downloads and sha256-verifies the exact pinned
`kcl-lang/cli` release that matches the `kcl-lang.io/cli` version function-kcl
v0.12.2 vendors (confirmed via its go.mod: `kcl-lang.io/cli v0.12.7`), and caches it
under KCL_CLI_CACHE_DIR (default: a per-user cache dir) so subsequent test runs are
instant and offline. Set KCL_CLI_BIN to point at an already-installed `kcl`/
`kclvm_cli` binary to skip the download entirely (e.g. air-gapped CI mirrors).

    python3 -m unittest discover -s tests -p 'test_*.py'
"""

import base64
import json
import os
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from hashlib import sha256

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PIPELINE_COMPOSITION = os.path.join(
    REPO_ROOT, "compositions", "local", "pipeline.yaml"
)


def ready_cicd_context(app_name, robot_id=42, robot_name=None, robot_secret=b"credential-a"):
    """Build the observed/required inputs for a converged CI credential stage."""
    if robot_name is None:
        robot_name = f"robot${app_name}+{app_name}-ci".encode()
    encoded_name = base64.b64encode(robot_name).decode("ascii")
    encoded_secret = base64.b64encode(robot_secret).decode("ascii")
    fingerprint = sha256(f"{encoded_name}:{encoded_secret}".encode()).hexdigest()
    description = (
        f"digiorg-managed harbor-robot-version={robot_id} "
        f"credential-sha256={fingerprint}"
    )

    def secret_observation(name):
        return {
            "Resource": {
                "status": {
                    "response": {
                        "statusCode": 200,
                        "body": json.dumps(
                            [{"name": name, "description": description, "created_at": "x"}]
                        ),
                    }
                }
            }
        }

    return {
        "ocds": {
            "harbor-robot": {
                "Resource": {
                    "status": {
                        "response": {
                            "statusCode": 200,
                            "body": json.dumps({"id": robot_id}),
                        }
                    }
                }
            },
            "gitea-secret-harbor-robot-name": secret_observation("HARBOR_ROBOT_NAME"),
            "gitea-secret-harbor-robot-secret": secret_observation("HARBOR_ROBOT_SECRET"),
        },
        "requiredResources": {
            "harborRobotCredential": [
                {
                    "Resource": {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": {
                            "name": f"{app_name}-harbor-robot",
                            "namespace": app_name,
                        },
                        "data": {"name": encoded_name, "secret": encoded_secret},
                    }
                }
            ]
        },
    }

# Pinned to match crossplane-contrib/function-kcl v0.12.2's own
# `kcl-lang.io/cli` dependency exactly (its go.mod pins `kcl-lang.io/cli
# v0.12.7`), so this test harness runs the same KCL language version the
# in-cluster function embeds. sha256 sums copied verbatim from the release's
# published `cli_0.12.7_checksums.txt`:
# https://github.com/kcl-lang/cli/releases/download/v0.12.7/cli_0.12.7_checksums.txt
KCL_CLI_VERSION = "0.12.7"
KCL_CLI_RELEASE_BASE = (
    "https://github.com/kcl-lang/cli/releases/download/v%s" % KCL_CLI_VERSION
)
KCL_CLI_ASSETS = {
    ("linux", "x86_64"): (
        "kcl-v%s-linux-amd64.tar.gz" % KCL_CLI_VERSION,
        "87a13210e1322327b70aa2482f3eaf195b135de6cc0126c40ec29abfeb578174",
    ),
    ("linux", "aarch64"): (
        "kcl-v%s-linux-arm64.tar.gz" % KCL_CLI_VERSION,
        "3dd3d1bc9f9a177a9e24d1f9c559f1c65f4cf1048dc636f9d8fa031b12807abb",
    ),
    ("darwin", "x86_64"): (
        "kcl-v%s-darwin-amd64.tar.gz" % KCL_CLI_VERSION,
        "be348eed7e5b93136343e97416c3459d557fbe059a0a0cbe28173d058faaae15",
    ),
    ("darwin", "arm64"): (
        "kcl-v%s-darwin-arm64.tar.gz" % KCL_CLI_VERSION,
        "f01f00f0f0ab26aa1fea1ff7dca856c9d9df064bd56e1570431e755ac6416abf",
    ),
    ("windows", "amd64"): (
        "kcl-v%s-windows-amd64.zip" % KCL_CLI_VERSION,
        "940c4e57515e93beec207bd98791bf6aa9bc12d1162d87bdd3593d7153e9c9cb",
    ),
    ("windows", "arm64"): (
        "kcl-v%s-windows-arm64.zip" % KCL_CLI_VERSION,
        "d5cb3e96e805fdf33e8b106b1f1e95ae05f16dd5a0ed68d74b38365aba863c93",
    ),
}

_ARCH_ALIASES = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "arm64": "arm64",
    "aarch64": "aarch64",
}


class KclCliNotFound(RuntimeError):
    pass


class KclCliChecksumMismatch(RuntimeError):
    pass


class KclRenderError(AssertionError):
    def __init__(self, proc):
        self.stdout = proc.stdout
        self.stderr = proc.stderr
        self.returncode = proc.returncode
        super().__init__(
            "KCL render failed (exit %d)\n--- stdout ---\n%s\n--- stderr ---\n%s"
            % (proc.returncode, proc.stdout, proc.stderr)
        )


def _cache_dir():
    configured = os.environ.get("KCL_CLI_CACHE_DIR")
    if configured:
        return configured
    return os.path.join(
        os.path.expanduser("~"), ".cache", "digiorg-core-catalog", "kcl-cli"
    )


def _current_platform_key():
    system = platform.system().lower()
    machine = _ARCH_ALIASES.get(platform.machine().lower(), platform.machine().lower())
    if system == "linux":
        return ("linux", "x86_64" if machine == "x86_64" else "aarch64")
    if system == "darwin":
        return ("darwin", "x86_64" if machine == "x86_64" else "arm64")
    if system == "windows":
        return ("windows", "amd64" if machine == "x86_64" else "arm64")
    return (system, machine)


def _sha256_of(path):
    digest = sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_and_verify(url, expected_sha256, dest_path):
    tmp_path = dest_path + ".part"
    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp_path, "wb") as out:
        shutil.copyfileobj(resp, out)
    actual = _sha256_of(tmp_path)
    if actual != expected_sha256:
        os.remove(tmp_path)
        raise KclCliChecksumMismatch(
            "downloaded %s sha256 %s does not match pinned %s -- refusing to use it"
            % (url, actual, expected_sha256)
        )
    os.replace(tmp_path, dest_path)


def _ensure_pinned_kcl_cli():
    """Download (once), sha256-verify, and cache the pinned kcl-lang/cli release
    for the current OS/arch. Returns the path to the extracted `kcl`/`kcl.exe`
    binary. Raises KclCliNotFound if this platform has no pinned asset.
    """
    key = _current_platform_key()
    if key not in KCL_CLI_ASSETS:
        raise KclCliNotFound(
            "no pinned kcl-lang/cli v%s asset for platform %r; set KCL_CLI_BIN "
            "to an existing kcl/kclvm_cli binary instead." % (KCL_CLI_VERSION, key)
        )
    asset_name, expected_sha256 = KCL_CLI_ASSETS[key]
    system = key[0]
    binary_name = "kcl.exe" if system == "windows" else "kcl"

    version_dir = os.path.join(_cache_dir(), KCL_CLI_VERSION)
    binary_path = os.path.join(version_dir, binary_name)
    if os.path.isfile(binary_path):
        return binary_path

    os.makedirs(version_dir, exist_ok=True)
    archive_path = os.path.join(version_dir, asset_name)
    if not os.path.isfile(archive_path) or _sha256_of(archive_path) != expected_sha256:
        _download_and_verify(
            "%s/%s" % (KCL_CLI_RELEASE_BASE, asset_name),
            expected_sha256,
            archive_path,
        )

    if archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(version_dir)
    else:
        with tarfile.open(archive_path) as tf:
            tf.extractall(version_dir)  # noqa: S202 -- pinned, checksum-verified archive

    if not os.path.isfile(binary_path):
        raise KclCliNotFound(
            "extracted kcl-lang/cli v%s but %s was not produced -- archive layout "
            "may have changed upstream" % (KCL_CLI_VERSION, binary_name)
        )
    st = os.stat(binary_path)
    os.chmod(binary_path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binary_path


def _find_kcl_cli():
    env_bin = os.environ.get("KCL_CLI_BIN")
    if env_bin:
        if os.path.isfile(env_bin):
            return env_bin
        raise KclCliNotFound("KCL_CLI_BIN=%s does not exist" % env_bin)
    found = shutil.which("kcl") or shutil.which("kclvm_cli")
    if found:
        return found
    return _ensure_pinned_kcl_cli()


def _subprocess_env():
    env = dict(os.environ)
    lib_dir = os.environ.get("KCL_CLI_LIBDIR")
    if lib_dir:
        env["LD_LIBRARY_PATH"] = lib_dir + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    return env


def load_pipeline_source(composition_path=PIPELINE_COMPOSITION):
    """Extract the embedded KCL source from the function-kcl pipeline step.

    This is the *only* place the KCL source is read from -- tests always exercise
    the exact text that ships in the Composition, never a copy.
    """
    with open(composition_path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    pipeline = doc["spec"]["pipeline"]
    step = next(s for s in pipeline if s["functionRef"]["name"] == "function-kcl")
    return step["input"]["spec"]["source"]


def render(params, composition_path=PIPELINE_COMPOSITION):
    """Render the Composition's KCL source against a synthetic function-kcl
    `params` document (`{"oxr": ..., "requiredResources": ..., "ctx": ...}`) and
    return the resulting `items` list exactly as function-kcl would return it
    to Crossplane.
    """
    source = load_pipeline_source(composition_path)
    kcl_bin = _find_kcl_cli()
    with tempfile.TemporaryDirectory() as tmp:
        kfile = os.path.join(tmp, "main.k")
        with open(kfile, "w", encoding="utf-8") as fh:
            fh.write(source)
        proc = subprocess.run(
            [kcl_bin, "run", kfile, "-D", "params=%s" % json.dumps(params)],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
            timeout=60,
        )
        if proc.returncode != 0:
            raise KclRenderError(proc)
        out = yaml.safe_load(proc.stdout) or {}
        return out.get("items", [])


def make_oxr(
    appName="myapp",
    team="platform-team",
    size="S",
    database=None,
    services=None,
    gitea=None,
    messaging=None,
):
    """Build a synthetic ObservedCompositeResource matching the Application XRD
    (core/crossplane/xrds/application.yaml), with the same defaults Crossplane
    would apply from the OpenAPI schema.
    """
    spec = {
        "appName": appName,
        "team": team,
        "size": size,
        "database": database if database is not None else {"enabled": False},
        "services": services if services is not None else [],
        "gitea": gitea
        if gitea is not None
        else {"enabled": False, "visibility": "private", "cicd": True},
        "messaging": messaging
        if messaging is not None
        else {"enabled": False, "subjects": []},
    }
    return {
        "apiVersion": "platform.digiorg.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": appName},
        "spec": spec,
        "status": {},
    }


def by_kind(items, kind):
    return [i for i in items if i.get("kind") == kind]


def manifests_of_kind(items, manifest_kind):
    """Return the inner `spec.forProvider.manifest` of every provider-kubernetes
    Object whose manifest.kind matches."""
    out = []
    for i in items:
        manifest = (i.get("spec") or {}).get("forProvider", {}).get("manifest")
        if manifest and manifest.get("kind") == manifest_kind:
            out.append(manifest)
    return out
