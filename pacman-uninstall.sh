#!/usr/bin/env bash
# Usage: ./pacman-uninstall.sh [keeppvc]. The default deletes the namespace.
set -euo pipefail
cd "$(dirname "$0")"
NAMESPACE="${NAMESPACE:-pacman-demo}"
MODE="${1:-}"
if [[ $# -gt 1 || ( -n "$MODE" && "$MODE" != keeppvc ) ]]; then
  echo "Usage: $0 [keeppvc]" >&2; exit 1
fi
if [[ ${#NAMESPACE} -gt 63 || ! "$NAMESPACE" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "Invalid namespace" >&2; exit 1
fi
command -v kubectl >/dev/null || { echo "kubectl not found" >&2; exit 1; }
owned=false
for object in deployment/pacman deployment/pacman-web deployment/pacman-score deployment/pacman-user \
    service/pacman service/pacman-web service/pacman-score service/pacman-user \
    deployment/mongo service/mongo configmap/mongo-init pvc/mongo-storage \
    "clusterrolebinding/pacman-clusterrole-$NAMESPACE"; do
  identity=$(kubectl -n "$NAMESPACE" get "$object" --ignore-not-found \
    -o 'go-template={{.metadata.name}}|{{index .metadata.labels "app.kubernetes.io/part-of"}}')
  if [[ -n "$identity" && "$identity" != "${object#*/}|pacman" ]]; then
    echo "Refusing conflicting $object in $NAMESPACE" >&2; exit 1
  fi
  [[ -z "$identity" ]] || owned=true
done
namespace=$(kubectl get namespace/"$NAMESPACE" --ignore-not-found \
  -o 'go-template={{.metadata.name}}|{{index .metadata.labels "app.kubernetes.io/part-of"}}')
if [[ -n "$namespace" ]]; then
  case "$namespace" in
    "$NAMESPACE|pacman") ;;
    "$NAMESPACE|"|"$NAMESPACE|<no value>")
      if [[ "$owned" == false ]]; then
        echo "No owned base resources in $NAMESPACE; refusing namespace cleanup" >&2; exit 1
      fi
      ;;
    *) echo "Conflicting namespace ownership in $NAMESPACE; refusing cleanup" >&2; exit 1 ;;
  esac
fi
# The ClusterRole is shared with installs in other namespaces; keep it.
kubectl delete clusterrolebinding "pacman-clusterrole-$NAMESPACE" --ignore-not-found
if [[ "$MODE" == keeppvc ]]; then
  kubectl -n "$NAMESPACE" delete service pacman pacman-web pacman-score pacman-user mongo --ignore-not-found
  kubectl -n "$NAMESPACE" delete deployment pacman pacman-web pacman-score pacman-user mongo \
    --ignore-not-found --cascade=foreground --wait=true --timeout=180s
  kubectl -n "$NAMESPACE" delete configmap mongo-init --ignore-not-found
  echo "Namespace, Mongo PVC and matching credential Secret retained."
  kubectl -n "$NAMESPACE" get pvc/mongo-storage --ignore-not-found -o name
else
  echo "Removing namespace '$NAMESPACE', including its PVC and credentials."
  kubectl delete namespace "$NAMESPACE" --ignore-not-found --wait=true --timeout=180s
fi
