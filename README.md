# Running Pac-Man on Kubernetes

Extended deployment surface for the Pac-Man app maintained at
[`saintdle/pacman`](https://github.com/saintdle/pacman). For a minimal,
in-repo set of learning examples (a multi-role `Deployment`, a postgres
example with a migration `Job`, Cilium L7 example, Prometheus and Tetragon
overlays) see
[`saintdle/pacman/k8s/examples`](https://github.com/saintdle/pacman/tree/master/k8s/examples).

This repository owns the Kubernetes platform surface: persistent storage,
Pod Security Standards, install scripts, and Cilium/Tetragon scenarios. Its raw
manifests and Kustomize examples are intentional teaching consumers that keep
resource names and lifecycle steps visible; they are not alternate Helm chart
sources. `saintdle/pacman` maintains the app and editable chart source, while
`saintdle/helm-charts` distributes versioned packages through the stable
<https://saintdle.github.io/helm-charts> repository.

<img src="https://veducate.co.uk/wp-content/uploads/2021/09/Pac-Man-UI.jpg" width="45%" height="45%">

## Pre-requisites

- A Kubernetes cluster with dynamic storage. `ServiceType: LoadBalancer`
  exposes the front-end on port `8080`. Without a LoadBalancer controller,
  leave the Service in place and use `kubectl port-forward`.
- Pod Security Standards labels are applied to the install namespace by
  `pacman-install.sh` (`enforce=baseline`, `audit/warn=restricted`). All
  base manifests are written to comply with the `restricted` profile
  (`runAsNonRoot`, dropped capabilities, `seccompProfile: RuntimeDefault`,
  `allowPrivilegeEscalation: false`).
- `bash`, `kubectl`, and `envsubst` on the local machine.

## Image

The raw manifests use app `0.7.16`, matching chart `0.7.17`, with all three
roles pinned to `docker.io/saintdle/pacman@sha256:592740129bce7530a347146b86647f8b516df3c19105aa4c2fe40b878840fccf`.
The released image includes the opt-in fault and synthetic security-probe
runtime; the chart keeps these controls disabled by default. Promotion updates
retain the shared image pin. The Cilium demo wrapper accepts a `PACMAN_IMAGE`
override.

## Install (base mongo-backed deployment)

```bash
./pacman-install.sh                  # defaults to namespace pacman-demo
NAMESPACE=my-ns ./pacman-install.sh  # custom namespace
```

The installer checks resource ownership before writing, waits for Mongo and
all three roles, and checks the web Service and score/user proxies. Re-running
it updates manifests while retaining the existing PVC and Mongo Secret.
If a retained PVC has lost its Secret, restore the matching credentials first;
the installer refuses to replace them with sample defaults. RBAC is rendered
against `$NAMESPACE`. A namespace containing demo-owned role resources is refused.

### Upgrade from the legacy install

The raw public endpoint changes from `pacman:80` to `pacman-web:8080`.
Prepare the split stack first:

```bash
./pacman-install.sh
kubectl -n pacman-demo get svc pacman pacman-web
kubectl -n pacman-demo port-forward svc/pacman-web 8080:8080
```

Both entry points remain available during preparation. Check the new
LoadBalancer address on port `8080` (or the port-forward), then update client
URLs, DNS, firewall and Ingress settings as applicable. Once access is verified:

```bash
./pacman-install.sh --retire-legacy
```

Use the same `NAMESPACE` override for both steps. Retirement rechecks the split
stack, then removes only `Deployment/pacman` and `Service/pacman`. Preparation
failures retain those resources. Mongo, its PVC and credentials are reused;
there is no data-copy migration. `kubectl apply -k .` is suitable for a fresh
install, but does not perform this legacy retirement.

The new LoadBalancer may receive a different address; continuous access and
address retention are not guaranteed. Before retiring legacy resources, save
their manifests. A rollback after retirement requires explicitly reapplying
those manifests against the same Mongo data and may allocate another address.

## Uninstall

```bash
./pacman-uninstall.sh                 # removes namespace, PVC and credentials
./pacman-uninstall.sh keeppvc         # keep namespace, PVC and matching Secret
NAMESPACE=my-ns ./pacman-uninstall.sh
```

Use `keeppvc` to demonstrate Mongo persistence: install, play a game,
record a high score, uninstall with `keeppvc`, install again, and the
high score is still there.

Both modes handle legacy, split and mixed installs. `keeppvc` waits for workload
shutdown, removes the app/Mongo Services, Deployments and init ConfigMap, and
retains the data and credentials. Neither mode removes the shared
`pacman-clusterrole`; only the selected namespace's ClusterRoleBinding is
removed. After all base installations are gone, an administrator may delete
that unused ClusterRole. Full uninstall deletes the PVC; backing-volume
retention or deletion depends on the StorageClass reclaim policy.

## Helm

A Helm chart is published from
[`saintdle/helm-charts`](https://github.com/saintdle/helm-charts) and
served via GitHub Pages at
[`saintdle.github.io/helm-charts`](https://saintdle.github.io/helm-charts/).
The chart packages the same modernized Pac-Man image used by the manifests
in this repo (pinned by digest, Pod Security Standards `restricted`),
and supports both the MongoDB and PostgreSQL backends.

Chart `0.7.17` defaults to an all-in-one app and Service port `80`. To use its
split roles, set `--set roles.enabled=true`. These raw-manifest migration and
uninstall scripts do not manage Helm releases; install Helm into a separate
namespace. Use `--version 0.7.17` to reproduce the app/chart pair above.

```bash
helm repo add veducate https://saintdle.github.io/helm-charts/
helm repo update veducate

# MongoDB backend (default)
helm install pacman veducate/pacman \
  -n pacman-demo --create-namespace

# PostgreSQL backend (runs a schema migration Job)
helm install pacman veducate/pacman \
  -n pacman-demo --create-namespace \
  --set database=postgres

# Inspect all available values
helm show values veducate/pacman
```

Common overrides:

```bash
# Expose via NodePort instead of LoadBalancer
helm install pacman veducate/pacman -n pacman-demo --create-namespace \
  --set service.type=NodePort

# Bring your own database secret
helm install pacman veducate/pacman -n pacman-demo --create-namespace \
  --set mongo.existingSecret=my-mongo-secret

# Enable Ingress
helm install pacman veducate/pacman -n pacman-demo --create-namespace \
  --set ingress.enabled=true \
  --set ingress.hosts[0].host=pacman.example.com \
  --set ingress.hosts[0].paths[0].path=/ \
  --set ingress.hosts[0].paths[0].pathType=Prefix
```

To uninstall:

```bash
helm uninstall pacman -n pacman-demo
kubectl -n pacman-demo delete pvc --all   # PVCs are retained by default
```

## Cilium demo suite

The [`cilium-demos`](cilium-demos) directory contains kind-based demos for
Cilium OSS, Hubble, Gateway API, Cluster Mesh, and Tetragon. The east-west
demo defaults to `pacman-east-west` and uses memory-backed roles. Other demos
default to `pacman-demo`; choose a separate namespace when the persistent base
is installed. The wrapper refuses persistent resources and conflicting role
ownership before applying workloads or policy.

```bash
./cilium-demos/scripts/setup-single-kind.sh
./cilium-demos/scripts/deploy-demo.sh microservices-east-west
NAMESPACE=pacman-east-west ./cilium-demos/scripts/deploy-demo.sh load-simulation
```

See [`cilium-demos/README.md`](cilium-demos/README.md) for the full
catalogue and per-demo walk-throughs.

## Architecture

The base install runs one container image as three roles, so the browser
only ever talks to `pacman-web` and score/user calls become real east-west
Service traffic that Cilium/Hubble can observe and policy-restrict at L7.
Score and user state both live in the shared Mongo, so high scores survive
re-installs (use `./pacman-uninstall.sh keeppvc`).

```
Namespace (pacman-demo)
├── Secret               mongodb-users-secret
├── ConfigMap            mongo-init      (mongo entrypoint user seed)
├── PVC                  mongo-storage
├── Deployment           mongo           (mongo:8, RWO PVC, Recreate strategy)
├── Service              mongo           (ClusterIP, 27017)
│
├── Deployment           pacman-web      (APP_ROLE=web,   DB_TYPE=memory, replicas: 2)
├── Service              pacman-web      (LoadBalancer, 8080 → http)
│        │
│        ├── SCORE_SERVICE_URL → http://pacman-score:8080
│        └── USER_SERVICE_URL  → http://pacman-user:8080
│
├── Deployment           pacman-score    (APP_ROLE=score, DB_TYPE=mongo, replicas: 1)
├── Service              pacman-score    (ClusterIP, 8080 → http)
│
├── Deployment           pacman-user     (APP_ROLE=user,  DB_TYPE=mongo, replicas: 1)
└── Service              pacman-user     (ClusterIP, 8080 → http)
```

ClusterRole / ClusterRoleBinding (cluster-scoped) grant `get/watch/list`
on `pods` and `nodes` to the `default` ServiceAccount of the install
namespace, used by the in-app location probe.

The entry-point Service is `pacman-web` on port `8080`. Without a
LoadBalancer, port-forward locally:

```bash
kubectl -n pacman-demo port-forward svc/pacman-web 8080:8080
```

## Repository layout

```
deployments/             Mongo and Pac-Man Deployments
services/                Mongo (ClusterIP) and Pac-Man (LoadBalancer) Services
persistentvolumeclaim/   Mongo data PVC
security/                RBAC (rendered at install time) and Secret
cilium-demos/            kind-based Cilium / Hubble / Tetragon demos
pacman-install.sh        Idempotent installer (NAMESPACE override supported)
pacman-uninstall.sh      Uninstaller with optional `keeppvc`
tests/                  Offline regressions and opt-in disposable-cluster checks
```

## Lifecycle validation

Run the offline regression suite with `bash tests/lifecycle.sh`. The cluster
suite requires Python 3 and an ARM64 Docker host. It is destructive within its
named disposable kind context and test namespaces; it must never target an existing application cluster:

```bash
kind create cluster --name pacman-role-integration --kubeconfig /tmp/pacman-role-test.kubeconfig
KUBECONFIG=/tmp/pacman-role-test.kubeconfig \
  PACMAN_TEST_CONTEXT=kind-pacman-role-integration bash tests/lifecycle-smoke.sh
```

It checks installs, legacy migration, retained credentials/data, demo isolation
using Kubernetes-native workloads, and both uninstall modes. It does not prove
external LoadBalancer access or Cilium/BPF enforcement. Those need separate
environment-specific validation. Delete the disposable cluster when finished.
