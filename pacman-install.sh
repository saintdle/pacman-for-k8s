#!/usr/bin/env bash
#
# Idempotent Pac-Man installer (extended deployment, classic Mongo backend).
# For the multi-role Cilium demos see cilium-demos/scripts/deploy-demo.sh.
#
# Honours $NAMESPACE (default: pacman-demo). RBAC is rendered from
# security/rbac.yaml so the ClusterRoleBinding subject matches $NAMESPACE.
set -euo pipefail

NAMESPACE="${NAMESPACE:-pacman-demo}"
export NAMESPACE

command -v kubectl   >/dev/null || { echo "kubectl not found"   >&2; exit 1; }
command -v envsubst  >/dev/null || { echo "envsubst not found (install gettext)" >&2; exit 1; }

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace "$NAMESPACE" \
  pod-security.kubernetes.io/enforce=baseline \
  pod-security.kubernetes.io/audit=restricted \
  pod-security.kubernetes.io/warn=restricted \
  --overwrite

envsubst < security/rbac.yaml             | kubectl apply -f -
kubectl apply -n "$NAMESPACE" -f security/secret.yaml
kubectl apply -n "$NAMESPACE" -f deployments/pacman-deployment.yaml
kubectl apply -n "$NAMESPACE" -f services/pacman-service.yaml

echo
echo "Install complete in namespace '$NAMESPACE'."
echo "  kubectl -n $NAMESPACE get pods,svc"
