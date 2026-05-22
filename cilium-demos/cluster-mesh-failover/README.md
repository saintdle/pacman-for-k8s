# Cluster Mesh failover demo

This demo uses two kind clusters connected with Cilium Cluster Mesh. `pacman-global` is exported as a global shared Service so traffic can fail over between clusters.

## Setup

```bash
../scripts/setup-clustermesh-kind.sh
```

Deploy the demo to both clusters:

```bash
kubectl config use-context kind-pacman-east
../scripts/deploy-demo.sh cluster-mesh-failover
kubectl config use-context kind-pacman-west
../scripts/deploy-demo.sh cluster-mesh-failover
```

## Fail over

```bash
./failover.sh
```

## Observe

```bash
cilium clustermesh status --context kind-pacman-east
cilium service list --context kind-pacman-east
hubble observe --namespace pacman-demo --follow
```

Expected result: the global service remains discoverable after one cluster's deployment is scaled down.
