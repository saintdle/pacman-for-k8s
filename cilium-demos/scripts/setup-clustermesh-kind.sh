#!/usr/bin/env bash
set -euo pipefail

CILIUM_VERSION="${CILIUM_VERSION:-1.19.4}"
CLUSTER_A="${CLUSTER_A:-pacman-east}"
CLUSTER_B="${CLUSTER_B:-pacman-west}"

create_cluster() {
  local name="$1"
  local config="$2"
  if ! kind get clusters | grep -qx "$name"; then
    kind create cluster --name "$name" --config "$config"
  fi
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
create_cluster "$CLUSTER_A" "$SCRIPT_DIR/../kind/cluster-east.yaml"
create_cluster "$CLUSTER_B" "$SCRIPT_DIR/../kind/cluster-west.yaml"

for cluster in "$CLUSTER_A" "$CLUSTER_B"; do
  kubectl config use-context "kind-$cluster"
  helm repo add cilium https://helm.cilium.io/ >/dev/null
  helm repo update >/dev/null
  helm upgrade --install cilium cilium/cilium \
    --version "$CILIUM_VERSION" \
    --namespace kube-system \
    --set kubeProxyReplacement=true \
    --set cluster.name="$cluster" \
    --set cluster.id=$([[ "$cluster" == "$CLUSTER_A" ]] && echo 1 || echo 2) \
    --set clustermesh.useAPIServer=true \
    --set hubble.relay.enabled=true \
    --set hubble.ui.enabled=true \
    --set ipam.mode=kubernetes
  cilium status --wait --context "kind-$cluster"
done

cilium clustermesh enable --context "kind-$CLUSTER_A" --service-type NodePort
cilium clustermesh enable --context "kind-$CLUSTER_B" --service-type NodePort
cilium clustermesh connect --context "kind-$CLUSTER_A" --destination-context "kind-$CLUSTER_B"
cilium clustermesh status --context "kind-$CLUSTER_A" --wait
cilium clustermesh status --context "kind-$CLUSTER_B" --wait
