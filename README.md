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

When a developer creates an `AppClaim`, Crossplane matches it to a Composition in this repo and
reconciles the requested resources (namespace, RBAC, database, services, Gitea repo, messaging).

## Structure

```
compositions/
  local/     # KinD / local development (Phase 1)
  aws/       # AWS EKS (Phase 3 — placeholder)
  azure/     # Azure AKS (Phase 2 — placeholder)
environments/
  local.yaml # EnvironmentConfig for KinD cluster
functions/
  size-resolver/  # KCL function: T-shirt size → concrete resource values
catalog/           # Backstage catalog templates (future)
```

### Compositions

Each target environment has its own directory with a `kustomization.yaml` that activates
individual Composition files as they are implemented:

| File | Issue | Description |
|------|-------|-------------|
| `base.yaml` | [#2](https://github.com/digiorg/core-catalog/issues/2) | Namespace, ServiceAccount, RBAC, NetworkPolicy |
| `database.yaml` | C-5 | CNPG PostgreSQL Cluster |
| `service.yaml` | C-6 | Deployment, Service, Ingress |
| `gitea.yaml` | C-7 | Gitea Repository + Actions Pipeline via provider-http |
| `messaging.yaml` | C-8 | NATS Subscription |

### Environments

`environments/local.yaml` is a Crossplane `EnvironmentConfig` that provides cluster-specific
values (registry, ingress class, storage class, domain, Gitea URL) to Compositions via
environment patches.

### Functions

`functions/size-resolver/` contains a KCL function that maps the `spec.size` T-shirt value
(`S`, `M`, `L`) from an `AppClaim` into concrete CPU, memory, storage, and replica values
used by the Compositions.

### Catalog

`catalog/` will hold Backstage software catalog templates for self-service app provisioning.

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
  whichever Compositions are listed (uncommented) under `resources:`.

## Local Development

```bash
# Preview what kustomize will apply
kubectl kustomize compositions/local/

# Apply directly (bypasses ArgoCD — for testing only)
kubectl apply -k compositions/local/
```
