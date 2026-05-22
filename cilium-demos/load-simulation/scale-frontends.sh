#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-pacman-demo}"
REPLICAS="${1:-6}"

kubectl -n "$NAMESPACE" scale deploy -l app.kubernetes.io/component=web --replicas="$REPLICAS"
kubectl -n "$NAMESPACE" rollout status deploy -l app.kubernetes.io/component=web --timeout=180s
kubectl -n "$NAMESPACE" get deploy,pods -l app.kubernetes.io/component=web -o wide
