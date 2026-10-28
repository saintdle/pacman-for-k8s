# Cilium demos for Pac-Man

These demos use the Pac-Man app as a small but real Kubernetes workload for showing Cilium OSS, Hubble, Gateway API, Cluster Mesh, and Tetragon.

Default image: `docker.io/saintdle/pacman@sha256:2a55f4d74bc05dea3ae05912fbdcbe9348f9aafb752902054a7f066cfa112f68`.
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
The released default image above contains the gated fault and synthetic probe
runtime used by the chart. The ten scenarios in
[`fault-demo`](fault-demo/README.md) are human-run teaching steps and have not
been validated as a complete live walkthrough. They are distinct from the
native workflow evidence below.

## Native BPF integration workflow

The public [native workflow run](https://github.com/saintdle/pacman-for-k8s/actions/runs/40221882676)
created an isolated Kind cluster on a native amd64 runner, applied the checked-in
east-west workloads and policy, and verified allow, explicit L7-deny, and
policy-removal recovery behavior. It also loaded the observe-only Tetragon
policy and verified file-open and child-process events correlated to the demo
pod. Every pull request reports `native-amd64`; relevant changes run the
native runtime scenario, and unrelated changes return an explicit no-op.
Manual and nightly runs retain full scenario coverage.

That run does not demonstrate Tetragon enforcement, all ten manual fault-demo
scenarios, or external LoadBalancer/provider behavior. The enforcement steps
remain exploratory and require a compatible native kernel; policy rendering or
loading alone is not proof that an action was enforced.
