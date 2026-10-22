# Cilium demos for Pac-Man

These demos use the Pac-Man app as a small but real Kubernetes workload for showing Cilium OSS, Hubble, Gateway API, Cluster Mesh, and Tetragon.

Default image: `docker.io/saintdle/pacman@sha256:592740129bce7530a347146b86647f8b516df3c19105aa4c2fe40b878840fccf`.
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
hubble observe --namespace pacman-east-west --follow
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
| [fault-demo](fault-demo/README.md) | Ten human-run Helm scenarios for faults, missing telemetry, and Tetragon observe/enforce |

Each demo directory has its own README with commands and observation points.
The east-west demo defaults to `pacman-east-west`; other demos keep the
`pacman-demo` default. Use `NAMESPACE` to select another isolated namespace.
The wrapper checks for persistent base resources and conflicting role ownership
before any namespace or workload change. Do not target the persistent base
namespace: the demo score/user roles use memory and do not preserve game data.
The Helm scenarios have their own app/chart prerequisites; the default image above does not include all of their features.

## Native BPF integration workflow

`.github/workflows/native-bpf.yml` runs manually or nightly on a native Ubuntu
amd64 runner. It creates an isolated Kind cluster, applies the checked-in
east-west workloads and policy, and verifies allow, explicit L7-deny, and
policy-removal recovery behavior. It then installs the checked-in observe-only
Tetragon policy and requires correlated file-open and child-process events from
the demo pod. The workflow is not a required pull-request check. Manual and
scheduled runs become available after the workflow reaches the default branch.

Static checks cannot prove that kernel BPF hooks ran; Cilium and Tetragon runtime
behavior remains unverified until a native hosted workflow run passes. The
Tetragon policy only observes events and does not enforce actions.
