# Cilium demos for Pac-Man

These demos use the Pac-Man app as a small but real Kubernetes workload for showing Cilium OSS, Hubble, Gateway API, Cluster Mesh, and Tetragon.

Default image: `docker.io/saintdle/pacman@sha256:d1c36678cd8cb7c4a0ea7c80f8161ec39c899da20c4328d0dc4ff21ada762198`.
Override it in scripts with `PACMAN_IMAGE=your-registry/your-image:tag`.

## Prerequisites

- `kind`
- `kubectl`
- `helm`
- `cilium` CLI
- Docker or another container runtime supported by kind

## Quick start

```bash
./cilium-demos/scripts/setup-single-kind.sh
./cilium-demos/scripts/deploy-demo.sh microservices-east-west
```

Then open Hubble UI or use:

```bash
cilium hubble port-forward &
hubble observe --namespace pacman-demo --follow
```

## Demo index

| Demo | What it shows |
|------|---------------|
| `microservices-east-west` | Real app roles talking over ClusterIP Services with Cilium L3/L4/L7 policy |
| `grpc-grpcroute` | gRPC service exposure through Cilium Gateway API GRPCRoute |
| `cluster-mesh-failover` | Dual-kind Cluster Mesh failover and global service flow observation |
| `topology-aware-routing` | Kubernetes topology-aware routing hints surfaced by the app |
| `canary-progressive-delivery` | Gateway API weighted routing with visible backend version/variant |
| `load-simulation` | Synthetic frontend users and k6 volume load for scaling and balancing demos |
| `fault-injection-resilience` | Gated app faults for resilience and observability demos |
| `tetragon-enforcement` | Tetragon tracing and optional enforcement for lab-only app probes |
| `runtime-security-incident` | Safe incident story using app logs, Tetragon events, and Hubble flows |

Each demo directory has its own README with commands and observation points.
