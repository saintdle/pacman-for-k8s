#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-pacman-demo}"

kubectl -n "$NAMESPACE" get pods -l app.kubernetes.io/component=load-generator -o wide
kubectl -n "$NAMESPACE" logs deploy/pacman-user-simulator --tail=40 --prefix=true || true
kubectl -n "$NAMESPACE" logs job/pacman-k6-load --tail=40 --prefix=true || true
