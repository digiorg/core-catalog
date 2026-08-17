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
| `spec.services[]` | Renders a `Deployment` + `Service` + `Ingress` for **every** entry (not index 0 only). Local application routes share the platform's existing `https://digiorg.local` TLS virtual host at `/apps/<appName>/<serviceName>/`; separate validated path segments avoid cross-app collisions when either name contains hyphens, and ingress-nginx strips that external prefix before proxying to the service root. Generated application Ingresses deliberately do not claim a namespaced TLS Secret because Core's central `digiorg.local` Ingress remains the single certificate owner. For a `build.enabled: true` service, the `Deployment` is withheld entirely until an image is actually promoted (see the automatic image promotion row below) — `spec.services[].image` is never used for such a service, even as a placeholder. |
| `spec.services[].build.enabled` (with `spec.gitea.enabled && spec.gitea.cicd`) | **Safe fresh-source scaffold and automatic immutable image promotion**: after an exact `200` observation confirms the expected `DigiOrg/<appName>` repository identity, a TLS-verified, Secret-file-mounted, hardened one-shot Job creates missing Dockerfiles for the requested build contexts in one create-only batch. Its `v2-r<revision>` identity uses an 80-bit SHA-256 prefix of the exact canonical scaffold payload: Crossplane-owned `spec.resourceRefs` updates and XR generation changes cannot rotate it, while any Dockerfile-relevant context, port, base-image, or scaffold-contract change creates a new revision. Existing source is preserved byte-for-byte, unsafe or malformed responses fail closed, and the workflow is withheld until an Observe-only Job gate confirms completion. The pipeline then observes Gitea's main branch HEAD and matching Harbor artifacts, pins Deployments to immutable digests, and preserves the previous promoted digest while a newer build is pending. Pulls use a separate pull-only Harbor robot and a least-privilege Secret synchronization path. |
| `spec.messaging.enabled` + `spec.messaging.subjects[]` | Renders a managed NATS JetStream `Stream` + `Consumer` (`jetstream.nats.io/v1beta2`, via the NACK controller — `core/apps/platform/nats-jetstream-controller.yaml`) for **every** subject, plus a `NATS_URL`/`NATS_SUBJECTS` ConfigMap |
| `spec.gitea.enabled` | Creates the Gitea source repository (`provider-http` `Request`, least-privilege token via `{{ crossplane-gitea-credentials:crossplane-system:token }}` secret placeholder — never a literal credential) with the requested `spec.gitea.visibility` |
| `spec.gitea.enabled && spec.gitea.cicd` | Additionally creates a `.gitea/workflows/ci.yaml` Gitea Actions matrix workflow (one job per `spec.services[]` entry with `build.enabled: true`, `actions/checkout` pinned to an immutable commit SHA, each pushing `<harborRegistry>/<appName>/<service name>:<gitea.sha>`; if no service opts in, an honest no-op placeholder job renders instead of a fake image), a least-privilege Harbor project, a project-scoped Harbor robot account (secret captured server-side via `secretInjectionConfigs` into a per-app Secret — the robot secret is never written into the Composition, a manifest, or Git), and pushes that robot's `name`/`secret` into the repository's Gitea Actions secrets `HARBOR_ROBOT_NAME`/`HARBOR_ROBOT_SECRET` (Gitea 1.23 `PUT /repos/{owner}/{repo}/actions/secrets/{secretname}`) so the generated workflow's Harbor login actually resolves |
| disabled capability | Renders **no** resources for that capability — no silent defaults |

### App-scoped Harbor images and fresh scaffolds

Every build-enabled application gets a Harbor project named `<appName>`, and
each build-enabled service publishes its image as `<appName>/<serviceName>`.
The generated workflow pushes a commit-SHA tag, but Deployments consume only
the corresponding immutable digest-pinned reference:

```text
digiorg.local/<appName>/<serviceName>@sha256:<digest>
```

Harbor visibility follows `spec.gitea.visibility` independently for each
application: `public` creates public project metadata and `private` creates
private project metadata. A private project can be absent from a nonmember's
Harbor UI even when it exists and is healthy. Verify project existence through
the app-scoped declarative `Request` conditions and API identity, together with
the digest-pinned Deployment image, rather than relying only on the UI project
list. This Composition does not grant Harbor project membership or weaken
private-project visibility.

For a newly scaffolded repository, the generated NGINX service responds with:

```text
DigiOrg - <appName>
```

Scaffolding remains create-only and commits all missing Dockerfiles atomically.
Existing repository files are never overwritten, so retained repositories keep
their current response until changed through their normal reviewed source
workflow (or recreated during an authoritative fresh reset).

### Environments

`environments/local.yaml` is a Crossplane `EnvironmentConfig` that provides cluster-specific
values (registry, ingress class, storage class, domain, Gitea URL). It is not
yet wired into `pipeline.yaml` via a `function-environment-configs` step —
the environment-specific endpoints the KCL script uses today are inline
constants. Wiring `EnvironmentConfig` through is tracked as follow-up
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
truncation); shared-host application subpaths, root/nested-path rewrites,
central TLS ownership, and distinct routes for multiple services; multiple
messaging subjects (no index-0 truncation, valid NACK
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

- The local shared-host Ingress contract transparently rewrites
  `/apps/<appName>/<serviceName>/...` to `/...` for ordinary root-based APIs.
  Applications that emit absolute redirects or asset URLs, scope cookies to a
  specific path, or otherwise need awareness of their external URL prefix must
  provide native base-path support; proxy rewriting cannot change browser-side
  routing semantics.

- No Azure/AWS Compositions exist yet (`compositions/aws`, `compositions/azure`
  remain placeholders).
