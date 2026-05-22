#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-pacman-cilium}"
CILIUM_VERSION="${CILIUM_VERSION:-1.19.4}"
GATEWAY_API_VERSION="${GATEWAY_API_VERSION:-v1.4.1}"

if ! kind get clusters | grep -qx "$CLUSTER_NAME"; then
  kind create cluster --name "$CLUSTER_NAME" --config "$(dirname "$0")/../kind/single-cluster.yaml"
fi

kubectl config use-context "kind-$CLUSTER_NAME"

kubectl apply -f "https://raw.githubusercontent.com/kubernetes-sigs/gateway-api/${GATEWAY_API_VERSION}/config/crd/standard/gateway.networking.k8s.io_gatewayclasses.yaml"
kubectl apply -f "https://raw.githubusercontent.com/kubernetes-sigs/gateway-api/${GATEWAY_API_VERSION}/config/crd/standard/gateway.networking.k8s.io_gateways.yaml"
kubectl apply -f "https://raw.githubusercontent.com/kubernetes-sigs/gateway-api/${GATEWAY_API_VERSION}/config/crd/standard/gateway.networking.k8s.io_httproutes.yaml"
kubectl apply -f "https://raw.githubusercontent.com/kubernetes-sigs/gateway-api/${GATEWAY_API_VERSION}/config/crd/standard/gateway.networking.k8s.io_grpcroutes.yaml"
kubectl apply -f "https://raw.githubusercontent.com/kubernetes-sigs/gateway-api/${GATEWAY_API_VERSION}/config/crd/standard/gateway.networking.k8s.io_referencegrants.yaml"

helm repo add cilium https://helm.cilium.io/ --force-update >/dev/null
helm repo update >/dev/null
helm upgrade --install cilium cilium/cilium \
  --version "$CILIUM_VERSION" \
  --namespace kube-system \
  --set kubeProxyReplacement=true \
  --set k8sServiceHost="${CLUSTER_NAME}-control-plane" \
  --set k8sServicePort=6443 \
  --set gatewayAPI.enabled=true \
  --set hubble.relay.enabled=true \
  --set hubble.ui.enabled=true \
  --set ipam.mode=kubernetes

cilium status --wait

helm repo add cilium https://helm.cilium.io/ --force-update >/dev/null
helm upgrade --install tetragon cilium/tetragon \
  --namespace kube-system \
  --create-namespace \
  --wait

kubectl label node "${CLUSTER_NAME}-worker" topology.kubernetes.io/zone=zone-a --overwrite || true
kubectl label node "${CLUSTER_NAME}-worker2" topology.kubernetes.io/zone=zone-b --overwrite || true
