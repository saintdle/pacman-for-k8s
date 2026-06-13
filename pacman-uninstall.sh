#!/usr/bin/env bash
#
# Pac-Man uninstaller. Usage:
#   ./pacman-uninstall.sh            # remove namespace 
#
# Honours $NAMESPACE (default: pacman-demo).
set -euo pipefail

NAMESPACE="${NAMESPACE:-pacman-demo}"
export NAMESPACE
MODE="${1:-}"

command -v kubectl  >/dev/null || { echo "kubectl not found"  >&2; exit 1; }
command -v envsubst >/dev/null || { echo "envsubst not found" >&2; exit 1; }

# Cluster-scoped RBAC, always removed (subject is namespaced).
envsubst < security/rbac.yaml | kubectl delete --ignore-not-found -f -

if [[ "$MODE" == "keeppvc" ]]; then
  echo "Removing workloads in '$NAMESPACE'..."
  kubectl -n "$NAMESPACE" delete --ignore-not-found -f services/pacman-service.yaml
  kubectl -n "$NAMESPACE" delete --ignore-not-found -f deployments/pacman-deployment.yaml
  kubectl -n "$NAMESPACE" delete --ignore-not-found -f security/secret.yaml
fi
