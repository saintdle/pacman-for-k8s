#!/usr/bin/env bash
# Prepare a persistent split install; retire legacy resources only on request.
set -euo pipefail
cd "$(dirname "$0")"
NAMESPACE="${NAMESPACE:-pacman-demo}"
export NAMESPACE
MODE="${1:-}"
if [[ $# -gt 1 || ( -n "$MODE" && "$MODE" != --retire-legacy ) ]]; then
  echo "Usage: $0 [--retire-legacy]" >&2; exit 1
fi
if [[ ${#NAMESPACE} -gt 63 || ! "$NAMESPACE" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "Invalid namespace" >&2; exit 1
fi
command -v kubectl >/dev/null || { echo "kubectl not found" >&2; exit 1; }
command -v envsubst >/dev/null || { echo "envsubst not found (install gettext)" >&2; exit 1; }

namespace=$(kubectl get namespace/"$NAMESPACE" --ignore-not-found \
  -o 'go-template={{.metadata.name}}|{{index .metadata.labels "app.kubernetes.io/part-of"}}')
case "$namespace" in
  ""|"$NAMESPACE|"|"$NAMESPACE|<no value>"|"$NAMESPACE|pacman") ;;
  *) echo "Refusing namespace owned by another application: $NAMESPACE" >&2; exit 1 ;;
esac

# Read failures and conflicting ownership must stop before the first write.
for object in deployment/pacman deployment/pacman-web deployment/pacman-score deployment/pacman-user \
    service/pacman service/pacman-web service/pacman-score service/pacman-user \
    deployment/mongo service/mongo configmap/mongo-init pvc/mongo-storage; do
  identity=$(kubectl -n "$NAMESPACE" get "$object" --ignore-not-found \
    -o 'go-template={{.metadata.name}}|{{index .metadata.labels "app.kubernetes.io/part-of"}}')
  if [[ -n "$identity" && "$identity" != "${object#*/}|pacman" ]]; then
    echo "Refusing conflicting $object in $NAMESPACE" >&2; exit 1
  fi
done
for object in clusterrole/pacman-clusterrole "clusterrolebinding/pacman-clusterrole-$NAMESPACE"; do
  identity=$(kubectl get "$object" --ignore-not-found \
    -o 'go-template={{.metadata.name}}|{{index .metadata.labels "app.kubernetes.io/part-of"}}')
  if [[ -n "$identity" && "$identity" != "${object#*/}|pacman" ]]; then
    echo "Refusing conflicting $object" >&2; exit 1
  fi
done
pvc=$(kubectl -n "$NAMESPACE" get pvc/mongo-storage --ignore-not-found -o name)
secret=$(kubectl -n "$NAMESPACE" get secret/mongodb-users-secret --ignore-not-found -o name)
if [[ -n "$pvc" && -z "$secret" ]]; then
  echo "Retained Mongo PVC needs its matching mongodb-users-secret. Restore that Secret first." >&2; exit 1
fi
if [[ -n "$secret" ]]; then
  valid=$(kubectl -n "$NAMESPACE" get secret/mongodb-users-secret -o 'go-template={{if and (index .data "database-user") (index .data "database-password") (index .data "database-name") (index .data "database-admin-name") (index .data "database-admin-password")}}valid{{end}}')
  [[ "$valid" == valid ]] || { echo "Mongo Secret is missing required keys" >&2; exit 1; }
fi

verify_split() {
  local role endpoints pod identity attempt
  for role in pacman-score pacman-user pacman-web; do
    kubectl -n "$NAMESPACE" rollout status deployment/"$role" --timeout=180s
  done
  endpoints=""
  for attempt in {1..15}; do
    endpoints=$(kubectl -n "$NAMESPACE" get endpointslices -l kubernetes.io/service-name=pacman-web \
      -o 'go-template={{range .items}}{{range .endpoints}}{{if .conditions.ready}}{{.targetRef.name}}{{"\n"}}{{end}}{{end}}{{end}}')
    [[ -n "$endpoints" ]] && break
    sleep 2
  done
  [[ -n "$endpoints" ]] || { echo "No ready pacman-web endpoints; legacy resources retained" >&2; return 1; }
  for pod in $endpoints; do
    identity=$(kubectl -n "$NAMESPACE" get pod/"$pod" -o 'go-template={{index .metadata.labels "app"}}')
    [[ "$identity" == pacman-web ]] || { echo "Unexpected web endpoint $pod; legacy resources retained" >&2; return 1; }
  done
  # Exercise the Service and both proxies without writing score/user data.
  # Pod readiness and Service routing can become visible at different times.
  for attempt in {1..10}; do
    if kubectl -n "$NAMESPACE" exec deployment/pacman-web -- node -e '
    (async () => {
      for (const path of ["/readyz", "/highscores/list", "/user/stats"]) {
        const res = await fetch("http://pacman-web:8080" + path, {signal: AbortSignal.timeout(5000)});
        if (!res.ok) throw new Error(path + ": HTTP " + res.status);
      }
    })().catch(err => { console.error(err.message); process.exit(1); });'; then
      return 0
    fi
    sleep 2
  done
  echo "Web Service/proxy checks failed; legacy resources retained" >&2
  return 1
}

if [[ "$MODE" == --retire-legacy ]]; then
  verify_split
  kubectl -n "$NAMESPACE" delete service pacman --ignore-not-found --wait=true --timeout=180s
  kubectl -n "$NAMESPACE" delete deployment pacman --ignore-not-found --cascade=foreground --wait=true --timeout=180s
  echo "Legacy resources retired. Public entry point: pacman-web:8080."
  exit 0
fi

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace "$NAMESPACE" \
  app.kubernetes.io/part-of=pacman \
  pod-security.kubernetes.io/enforce=baseline \
  pod-security.kubernetes.io/audit=restricted \
  pod-security.kubernetes.io/warn=restricted --overwrite
envsubst '${NAMESPACE}' < security/rbac.yaml | kubectl apply -f -
if [[ -z "$secret" ]]; then
  kubectl -n "$NAMESPACE" create -f security/secret.yaml
fi
kubectl -n "$NAMESPACE" apply -f persistentvolumeclaim/mongo-pvc.yaml
kubectl -n "$NAMESPACE" apply -f deployments/mongo-deployment.yaml
kubectl -n "$NAMESPACE" apply -f services/mongo-service.yaml
kubectl -n "$NAMESPACE" rollout status deployment/mongo --timeout=180s
kubectl -n "$NAMESPACE" apply -f deployments/pacman-deployment.yaml
kubectl -n "$NAMESPACE" apply -f services/pacman-service.yaml
verify_split

echo "Install prepared in namespace '$NAMESPACE'."
echo "  kubectl -n $NAMESPACE port-forward svc/pacman-web 8080:8080"
legacy=$(kubectl -n "$NAMESPACE" get deployment/pacman service/pacman --ignore-not-found -o name)
if [[ -n "$legacy" ]]; then
  echo "Legacy pacman:80 is retained. Verify the new public address on :8080 and update clients."
  echo "Then explicitly retire legacy resources: NAMESPACE=$NAMESPACE $0 --retire-legacy"
fi
