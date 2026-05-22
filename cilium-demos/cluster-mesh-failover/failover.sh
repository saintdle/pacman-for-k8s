#!/usr/bin/env bash
set -euo pipefail

CLUSTER_A="${CLUSTER_A:-pacman-east}"
CLUSTER_B="${CLUSTER_B:-pacman-west}"
NAMESPACE="${NAMESPACE:-pacman-demo}"

kubectl --context "kind-$CLUSTER_A" -n "$NAMESPACE" scale deploy/pacman-global --replicas=0
kubectl --context "kind-$CLUSTER_A" -n "$NAMESPACE" rollout status deploy/pacman-global --timeout=120s || true
kubectl --context "kind-$CLUSTER_B" -n "$NAMESPACE" get endpointslices -l kubernetes.io/service-name=pacman-global -o wide
