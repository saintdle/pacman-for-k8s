# Running Pac-Man on Kubernetes

Extended deployment surface for the Pac-Man app maintained at
[`saintdle/pacman`](https://github.com/saintdle/pacman). For a minimal,
in-repo set of learning examples (a multi-role `Deployment`, a postgres
example with a migration `Job`, Cilium L7 example, Prometheus and Tetragon
overlays) see
[`saintdle/pacman/k8s/examples`](https://github.com/saintdle/pacman/tree/master/k8s/examples).

This repo layers on persistent storage, Pod Security Standards, install
scripts, and a catalogue of Cilium demos.

<img src="https://veducate.co.uk/wp-content/uploads/2021/09/Pac-Man-UI.jpg" width="45%" height="45%">

## Pre-requisites

- A Kubernetes cluster (any distribution). `ServiceType: LoadBalancer` is
  recommended for external connectivity to the front-end. Without it, edit
  `services/pacman-service.yaml` to use `NodePort` or `ClusterIP` and use
  `kubectl port-forward`.
- Pod Security Standards labels are applied to the install namespace by
  `pacman-install.sh` (`enforce=baseline`, `audit/warn=restricted`). All
  base manifests are written to comply with the `restricted` profile
  (`runAsNonRoot`, dropped capabilities, `seccompProfile: RuntimeDefault`,
  `allowPrivilegeEscalation: false`).
- `bash`, `kubectl`, and `envsubst` on the local machine.

## Image

All manifests in this repo target `docker.io/saintdle/pacman`, pinned by
digest (`sha256:d1c36678...`). The Cilium demos accept a `PACMAN_IMAGE`
env var override.

## Install (base mongo-backed deployment)

```bash
./pacman-install.sh                  # defaults to namespace pacman-demo
NAMESPACE=my-ns ./pacman-install.sh  # custom namespace
```

The install is idempotent (uses `kubectl apply`). Re-running it picks up
manifest changes without errors. RBAC is rendered against the chosen
`$NAMESPACE` so the ClusterRoleBinding subject matches the namespace you
installed into.

## Uninstall

```bash
./pacman-uninstall.sh                 # removes everything including the PVC
./pacman-uninstall.sh keeppvc         # keep the namespace and the PVC
NAMESPACE=my-ns ./pacman-uninstall.sh
```

Use `keeppvc` to demonstrate Mongo persistence: install, play a game,
record a high score, uninstall with `keeppvc`, install again, and the
high score is still there.

## Helm

A Helm chart is published from
[`saintdle/helm-charts`](https://github.com/saintdle/helm-charts) and
served via GitHub Pages at
[`saintdle.github.io/helm-charts`](https://saintdle.github.io/helm-charts/).
The chart packages the same modernized Pac-Man image used by the manifests
in this repo (pinned by digest, Pod Security Standards `restricted`),
and supports both the MongoDB and PostgreSQL backends.

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
Cilium OSS, Hubble, Gateway API, Cluster Mesh, and Tetragon. The default
namespace is `pacman-demo`.

```bash
./cilium-demos/scripts/setup-single-kind.sh
./cilium-demos/scripts/deploy-demo.sh microservices-east-west
./cilium-demos/scripts/deploy-demo.sh load-simulation
```

See [`cilium-demos/README.md`](cilium-demos/README.md) for the full
catalogue and per-demo walk-throughs.

## Architecture

```
Namespace (pacman-demo)
├── Secret               mongodb-users-secret
├── ConfigMap            mongo-init   (mongo entrypoint user seed)
├── PVC                  mongo-storage
├── Deployment           mongo        (mongo:8, RWO PVC, Recreate strategy)
├── Service              mongo        (ClusterIP, 27017)
├── Deployment           pacman       (saintdle/pacman, /healthz, /readyz)
└── Service              pacman       (LoadBalancer, 80 → 8080)
```

ClusterRole / ClusterRoleBinding (cluster-scoped) grant `get/watch/list`
on `pods` and `nodes` to the `default` ServiceAccount of the install
namespace, used by the in-app location probe.

## Repository layout

```
deployments/             Mongo and Pac-Man Deployments
services/                Mongo (ClusterIP) and Pac-Man (LoadBalancer) Services
persistentvolumeclaim/   Mongo data PVC
security/                RBAC (rendered at install time) and Secret
cilium-demos/            kind-based Cilium / Hubble / Tetragon demos
pacman-install.sh        Idempotent installer (NAMESPACE override supported)
pacman-uninstall.sh      Uninstaller with optional `keeppvc`
```
