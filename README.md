# digiorg/core-catalog

Crossplane Compositions for DigiOrg App Templates.

## Architecture

DigiOrg's platform is split across two repositories:

| Repo | Responsibility |
|------|---------------|
| `digiorg/core` | XRDs, Providers, ProviderConfigs, platform infrastructure (ArgoCD, cert-manager, ingress, etc.) |
| `digiorg/core-catalog` *(this repo)* | Crossplane **Compositions** — the implementations that fulfil AppClaims |

The XRD (`platform.digiorg.io/v1alpha1` / `Application` / `AppClaim`) lives in
[`digiorg/core` → `crossplane/xrds/application.yaml`](https://github.com/digiorg/core/blob/main/crossplane/xrds/application.yaml).

When a developer creates an `AppClaim`, Crossplane matches it to the single
Composition in this repo and reconciles the requested resources (namespace,
RBAC, database, services, Gitea repo/CI, Harbor project/robot, messaging).

## Structure

```
compositions/
  local/     # KinD / local development (Phase 1) -- pipeline.yaml
  aws/       # AWS EKS (Phase 3 — placeholder)
  azure/     # Azure AKS (Phase 2 — placeholder)
environments/
  local.yaml # EnvironmentConfig for KinD cluster
functions/
  size-resolver/  # superseded -- sizing now lives inline in pipeline.yaml (see below)
tests/       # Python render tests against the real KCL runtime (see Testing)
catalog/           # Backstage catalog templates (future)
```

### Compositions (Issue #285)

`compositions/local/pipeline.yaml` is the **single deterministic Composition**
for the local target — it replaces the five earlier competing Compositions
(`base.yaml`, `database.yaml`, `service.yaml`, `gitea.yaml`, `messaging.yaml`),
which each matched the same `Application` XR and therefore could not be
combined to fulfil one AppClaim requesting several capabilities at once.

It is a two-step Crossplane v2 Pipeline `mode: Pipeline` Composition:

1. **`render`** — `crossplane-contrib/function-kcl` (pinned
   `core/crossplane/providers/packages/function-kcl.yaml`). The entire
   rendering contract — conditionals, iteration, sizing, and the CNPG
   fail-closed gate — is one embedded KCL script (`spec.source`), which is
   the single source of truth for both production behavior and this repo's
   tests (see Testing below).
2. **`automatically-detect-ready-composed-resources`** —
   `crossplane-contrib/function-auto-ready` (pinned
   `core/crossplane/providers/packages/function-auto-ready.yaml`), so the
   AppClaim's `Ready` condition reflects the real readiness of every composed
   resource instead of defaulting to unready forever.

What the KCL script actually renders, per `AppClaim`/`Application` field:

| Field | Behavior |
|---|---|
| *(always)* | `Namespace`, `ServiceAccount`, `Role`, `RoleBinding`, `NetworkPolicy` for `spec.appName` |
| `spec.database.enabled` | Requests `RequiredResources` for the CNPG CRD (`clusters.postgresql.cnpg.io`) and `ValidatingWebhookConfiguration` (`cnpg-validating-webhook-configuration`). If **not** both present, renders **no** `Cluster` and sets a `DatabaseReady=False` Condition (`CnpgPrerequisiteNotReady`) on the composite **and** claim naming the exact prerequisite (`nu scripts/local-setup.nu future-infra`). Only creates the CNPG `Cluster` once both are confirmed present — never coupled to the internal platform database. |
| `spec.services[]` | Renders a `Deployment` + `Service` + `Ingress` for **every** entry (not index 0 only). For a `build.enabled: true` service, the `Deployment` is withheld entirely until an image is actually promoted (see the automatic image promotion row below) — `spec.services[].image` is never used for such a service, even as a placeholder. |
| `spec.services[].build.enabled` (with `spec.gitea.enabled && spec.gitea.cicd`) | **Automatic immutable image promotion**: the pipeline observes Gitea's main branch HEAD commit and, per service, whether Harbor already has an artifact tagged with that exact commit SHA. Only once both are confirmed does it pin that service's `Deployment` to the artifact's immutable `<harborRegistry>/<appName>/<service name>@sha256:<digest>` reference — never a mutable tag — and record `headSha`/`digest`/`image` in the composite's own `status.services[]`. A newer HEAD whose build hasn't been pushed yet is a routine pending state: the previously promoted digest is kept (both in status and the running `Deployment`) rather than cleared. Pulls use a separate, pull-only Harbor robot (never the push-capable CI robot above): its raw `name`/`secret` are captured into an intermediate Opaque Secret via provider-http (never constructed by provider-http itself — that would defeat its own response redaction and per-field missing-value handling), then a dedicated, least-privilege provider-kubernetes Job builds the typed `kubernetes.io/dockerconfigjson` Secret out-of-band (shell + `base64`, credential piped to `kubectl apply -f -` over stdin, never argv/logs) and an Observe-only `Object` confirms the real credential actually landed before the `Deployment` (which references it via `imagePullSecrets`) is allowed to render. |
| `spec.messaging.enabled` + `spec.messaging.subjects[]` | Renders a managed NATS JetStream `Stream` + `Consumer` (`jetstream.nats.io/v1beta2`, via the NACK controller — `core/apps/platform/nats-jetstream-controller.yaml`) for **every** subject, plus a `NATS_URL`/`NATS_SUBJECTS` ConfigMap |
| `spec.gitea.enabled` | Creates the Gitea source repository (`provider-http` `Request`, least-privilege token via `{{ crossplane-gitea-credentials:crossplane-system:token }}` secret placeholder — never a literal credential) with the requested `spec.gitea.visibility` |
| `spec.gitea.enabled && spec.gitea.cicd` | Additionally creates a `.gitea/workflows/ci.yaml` Gitea Actions matrix workflow (one job per `spec.services[]` entry with `build.enabled: true`, `actions/checkout` pinned to an immutable commit SHA, each pushing `<harborRegistry>/<appName>/<service name>:<gitea.sha>`; if no service opts in, an honest no-op placeholder job renders instead of a fake image), a least-privilege Harbor project, a project-scoped Harbor robot account (secret captured server-side via `secretInjectionConfigs` into a per-app Secret — the robot secret is never written into the Composition, a manifest, or Git), and pushes that robot's `name`/`secret` into the repository's Gitea Actions secrets `HARBOR_ROBOT_NAME`/`HARBOR_ROBOT_SECRET` (Gitea 1.23 `PUT /repos/{owner}/{repo}/actions/secrets/{secretname}`) so the generated workflow's Harbor login actually resolves |
| disabled capability | Renders **no** resources for that capability — no silent defaults |

### Environments

`environments/local.yaml` is a Crossplane `EnvironmentConfig` that provides cluster-specific
values (registry, ingress class, storage class, domain, Gitea URL). It is not
yet wired into `pipeline.yaml` via a `function-environment-configs` step —
the URLs the KCL script uses today are inline constants
(`gitea-http.gitea.svc.cluster.local`, `harbor-core.harbor.svc.cluster.local`,
`nats.messaging.svc.cluster.local`) matching the in-cluster Service DNS names
in `digiorg/core`. Wiring `EnvironmentConfig` through is tracked as follow-up
work for the AWS/Azure targets, which will need per-environment values.

### Functions

`functions/size-resolver/` was a planned second Composition Function
(KCL-based T-shirt sizing). Issue #285's single-pipeline requirement folded
that logic directly into `compositions/local/pipeline.yaml`'s `sizeTable`
rather than adding a second function hop; the directory is kept only as a
historical note.

### Catalog

`catalog/` will hold Backstage software catalog templates for self-service app provisioning.

## Testing

`tests/` contains real render tests, not a hand-rolled simulation of KCL
semantics: `tests/render_harness.py` extracts the exact KCL source embedded in
`pipeline.yaml` and executes it with the actual KCL language runtime
(`kcl-lang/cli`, pinned to the same version — v0.12.7 — that
`crossplane-contrib/function-kcl` v0.12.2 vendors), against synthetic
`function-kcl` `params` documents (`oxr`, `requiredResources`).

The harness downloads and sha256-verifies that exact pinned CLI release on
first run and caches it (`KCL_CLI_CACHE_DIR`, default `~/.cache/digiorg-core-catalog/kcl-cli`)
so it is fully reproducible without any pre-installed global tool; set
`KCL_CLI_BIN` to point at an existing `kcl`/`kclvm_cli` binary instead (e.g.
air-gapped mirrors).

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Coverage: base resources always present; database enabled/disabled and the
CNPG fail-closed gate (missing prerequisite, partial prerequisite, ready);
no internal-platform-database coupling; multiple services (no index-0
truncation); multiple messaging subjects (no index-0 truncation, valid NACK
identifiers); Gitea visibility (private/public) and `cicd` true/false;
idempotent observe-before-create mappings for Gitea/Harbor; Harbor robot
secret injection (never a literal secret); no literal credentials anywhere in
the Composition source; exactly one Composition file/two pipeline steps.

## Phase Roadmap

| Phase | Target | Status |
|-------|--------|--------|
| Phase 1 | local / KinD | In Progress |
| Phase 2 | Azure AKS | Planned |
| Phase 3 | AWS EKS | Planned |

## ArgoCD Integration

`digiorg/core` manages this repository as an ArgoCD Application:

- **File**: `apps/platform/core-catalog.yaml` (renamed from `core-app-catalog.yaml` — see [digiorg/core#254](https://github.com/digiorg/core/issues/254))
- **Sync Wave**: 8 (after XRDs and Providers are ready)
- ArgoCD applies the `compositions/local/kustomization.yaml` to the cluster, which activates
  `pipeline.yaml`.

## Local Development

```bash
# Preview what kustomize will apply
kubectl kustomize compositions/local/

# Apply directly (bypasses ArgoCD — for testing only)
kubectl apply -k compositions/local/
```

## Known limitations

- `EnvironmentConfig` (`environments/local.yaml`) is not yet consumed by
  `pipeline.yaml` — see Environments above.
- The CNPG readiness gate checks CRD + `ValidatingWebhookConfiguration`
  *existence*, not their live `status.conditions` (e.g. CRD `Established`).
  This is a strong, name-addressable proxy (both only exist once the operator
  Helm chart has been installed via `future-infra`) but is not a byte-for-byte
  match for the deeper `wait_for_cnpg_webhook_ready` liveness check
  `scripts/local-setup.nu` performs during `future-infra` itself.
- The generated Gitea Actions CI workflow builds and pushes immutably-tagged
  images to Harbor for every `build.enabled` service; the pipeline Composition
  itself (not GitOps/an image-updater controller) observes Gitea HEAD and the
  matching Harbor artifact and promotes the Deployment's image automatically
  — see the automatic image promotion row above. `spec.services[].image` is
  therefore only meaningful for services that never set `build.enabled: true`.
- CI only builds and pushes images; it does not yet scaffold Dockerfiles or
  other build-context source files into the Gitea repository for a service
  that opts into `build.enabled` — the repository owner is expected to add
  their own build context (a Dockerfile at `services[].build.context`,
  default the repository root).
- No Azure/AWS Compositions exist yet (`compositions/aws`, `compositions/azure`
  remain placeholders).
