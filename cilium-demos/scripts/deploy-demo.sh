#!/usr/bin/env bash
set -euo pipefail

DEMO="${1:?usage: deploy-demo.sh <demo-directory>}"
NAMESPACE="${NAMESPACE:-pacman-demo}"
PACMAN_IMAGE="${PACMAN_IMAGE:-docker.io/saintdle/pacman@sha256:cb9272461127465676fefd4778e67faea41ef65e03c19014626287d2c542dd05}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RENDER_DIR="$(mktemp -d "$ROOT/.deploy-render.XXXXXX")"
trap 'rm -rf "$RENDER_DIR"' EXIT

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace "$NAMESPACE" pod-security.kubernetes.io/enforce=baseline --overwrite
cat > "$RENDER_DIR/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: $NAMESPACE
resources:
  - ../$DEMO
EOF
kubectl apply -k "$RENDER_DIR"

deployments=$(kubectl -n "$NAMESPACE" get deploy -l app.kubernetes.io/part-of=pacman-cilium-demo -o name)

for deploy in $deployments; do
  kubectl -n "$NAMESPACE" set image "$deploy" '*='"$PACMAN_IMAGE" >/dev/null || true
done

for deploy in $deployments; do
  kubectl -n "$NAMESPACE" rollout status "$deploy" --timeout=180s
done
kubectl -n "$NAMESPACE" get pods,svc,gateway,httproute,grpcroute 2>/dev/null || kubectl -n "$NAMESPACE" get pods,svc
