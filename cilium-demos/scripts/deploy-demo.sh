#!/usr/bin/env bash
set -euo pipefail

DEMO="${1:?usage: deploy-demo.sh <demo-directory>}"
if [[ "$DEMO" == microservices-east-west ]]; then
  NAMESPACE="${NAMESPACE:-pacman-east-west}"
else
  NAMESPACE="${NAMESPACE:-pacman-demo}"
fi
PACMAN_IMAGE="${PACMAN_IMAGE:-docker.io/saintdle/pacman@sha256:592740129bce7530a347146b86647f8b516df3c19105aa4c2fe40b878840fccf}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[[ "$DEMO" =~ ^[a-z0-9-]+$ && -d "$ROOT/$DEMO" ]] || { echo "Unknown demo" >&2; exit 1; }
if [[ ${#NAMESPACE} -gt 63 || ! "$NAMESPACE" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "Invalid namespace" >&2; exit 1
fi
# Guard overrides too, before creating/labeling the namespace or applying policy.
namespace=$(kubectl get namespace/"$NAMESPACE" --ignore-not-found \
  -o 'go-template={{.metadata.name}}|{{index .metadata.labels "app.kubernetes.io/part-of"}}')
case "$namespace" in
  ""|"$NAMESPACE|"|"$NAMESPACE|<no value>"|"$NAMESPACE|pacman-cilium-demo") ;;
  *) echo "Conflicting namespace ownership in $NAMESPACE" >&2; exit 1 ;;
esac
for object in pvc/mongo-storage secret/mongodb-users-secret deployment/mongo service/mongo configmap/mongo-init \
    deployment/pacman service/pacman; do
  found=$(kubectl -n "$NAMESPACE" get "$object" --ignore-not-found -o name)
  [[ -z "$found" ]] || { echo "Persistent/base resource $object in $NAMESPACE; choose an isolated demo namespace" >&2; exit 1; }
done
LEGACY_SERVICE_TEMPLATE='go-template={{.metadata.name}}|{{index .metadata.labels "app.kubernetes.io/part-of"}}|{{index .metadata.labels "app.kubernetes.io/name"}}|{{index .metadata.labels "app.kubernetes.io/component"}}|{{len .metadata.labels}}|{{.spec.type}}|{{len .spec.selector}}|{{index .spec.selector "app.kubernetes.io/name"}}|{{index .spec.selector "app.kubernetes.io/component"}}|{{len .spec.ports}}|{{range .spec.ports}}{{.name}},{{.port}},{{.targetPort}},{{.protocol}}{{end}}'
for role in web score user; do
  name="pacman-$role"
  deployment_identity=$(kubectl -n "$NAMESPACE" get "deployment/$name" --ignore-not-found \
    -o 'go-template={{.metadata.name}}|{{index .metadata.labels "app.kubernetes.io/part-of"}}')
  if [[ -n "$deployment_identity" && "$deployment_identity" != "$name|pacman-cilium-demo" ]]; then
    echo "Conflicting deployment/$name in $NAMESPACE; refusing to overwrite it" >&2; exit 1
  fi
  service_identity=$(kubectl -n "$NAMESPACE" get "service/$name" --ignore-not-found \
    -o 'go-template={{.metadata.name}}|{{index .metadata.labels "app.kubernetes.io/part-of"}}')
  if [[ -z "$service_identity" || "$service_identity" == "$name|pacman-cilium-demo" ]]; then
    continue
  fi
  if [[ "$service_identity" != "$name|<no value>" ]]; then
    echo "Conflicting service/$name in $NAMESPACE; refusing to overwrite it" >&2; exit 1
  fi
  if [[ "$deployment_identity" != "$name|pacman-cilium-demo" ]]; then
    echo "Unowned deployment for legacy service/$name in $NAMESPACE; refusing to adopt it" >&2; exit 1
  fi
  service_shape=$(kubectl -n "$NAMESPACE" get "service/$name" --ignore-not-found \
    -o "$LEGACY_SERVICE_TEMPLATE")
  expected_shape="$name|<no value>|pacman|$role|2|ClusterIP|2|pacman|$role|1|http,8080,http,TCP"
  if [[ "$service_shape" != "$expected_shape" ]]; then
    echo "Unknown legacy service/$name shape in $NAMESPACE; refusing to adopt it" >&2; exit 1
  fi
done
RENDER_DIR="$(mktemp -d "$ROOT/.deploy-render.XXXXXX")"
trap 'rm -rf "$RENDER_DIR"' EXIT

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace "$NAMESPACE" app.kubernetes.io/part-of=pacman-cilium-demo \
  pod-security.kubernetes.io/enforce=baseline --overwrite
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
