#!/usr/bin/env bash
# Helm cleanup fixture; destructive only in its dedicated disposable Kind cluster.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${KUBECONFIG:?Supply the dedicated disposable-cluster kubeconfig}"
[[ "${PACMAN_HELM_TEST_CONTEXT:-}" == kind-pacman-helm-cleanup ]] || {
  echo 'PACMAN_HELM_TEST_CONTEXT must be kind-pacman-helm-cleanup' >&2; exit 1;
}
[[ "$(kubectl config current-context)" == "$PACMAN_HELM_TEST_CONTEXT" ]] || {
  echo 'Current context does not match PACMAN_HELM_TEST_CONTEXT' >&2; exit 1;
}
server=$(kubectl config view --minify -o 'jsonpath={.clusters[0].cluster.server}')
[[ "$server" == https://127.0.0.1:* ]] || { echo 'Expected a local Kind API server' >&2; exit 1; }
nodes=$(kubectl --context "$PACMAN_HELM_TEST_CONTEXT" get nodes -o json)
python3 - "$nodes" <<'PY'
import json, sys
nodes = json.loads(sys.argv[1]).get('items', [])
assert nodes and all(n['metadata']['name'].startswith('pacman-helm-cleanup-') and
                     n['status']['nodeInfo']['architecture'] == 'arm64' for n in nodes)
PY

suffix=$(python3 -c 'import secrets; print(secrets.token_hex(4))')
namespace="helm-cleanup-$suffix"
release="pacman-cleanup-$suffix"
existing_namespace=$(kubectl --context "$PACMAN_HELM_TEST_CONTEXT" get namespace "$namespace" \
  --ignore-not-found -o name)
if [[ -n "$existing_namespace" ]]; then
  echo 'Generated fixture namespace already exists' >&2; exit 1
fi
fixture_dir=$(mktemp -d)
cleanup() {
  kubectl --context "$PACMAN_HELM_TEST_CONTEXT" delete namespace "$namespace" \
    --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1 || true
  rm -rf "$fixture_dir"
}
trap cleanup EXIT
chart="$fixture_dir/pacman"
mkdir -p "$chart/templates"
cat > "$chart/Chart.yaml" <<'EOF'
apiVersion: v2
name: pacman
version: 1.2.3
appVersion: "1.2.3"
home: https://github.com/saintdle/pacman
sources:
  - https://github.com/saintdle/pacman-for-k8s
EOF
cat > "$chart/templates/pvc.yaml" <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ .Release.Name }}-data
  labels:
    app.kubernetes.io/part-of: pacman
    app.kubernetes.io/instance: {{ .Release.Name }}
    app.kubernetes.io/managed-by: Helm
    helm.sh/chart: pacman-1.2.3
  annotations:
    "helm.sh/resource-policy": keep
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Mi
EOF
cat > "$chart/templates/secret.yaml" <<'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: {{ .Release.Name }}-generated
  labels:
    app.kubernetes.io/part-of: pacman
    app.kubernetes.io/instance: {{ .Release.Name }}
    app.kubernetes.io/managed-by: Helm
    helm.sh/chart: pacman-1.2.3
  annotations:
    "helm.sh/resource-policy": keep
type: Opaque
stringData:
  fixture: harmless-test-value
EOF

helm --kube-context "$PACMAN_HELM_TEST_CONTEXT" install "$release" "$chart" \
  --namespace "$namespace" --create-namespace >/dev/null
kubectl --context "$PACMAN_HELM_TEST_CONTEXT" -n "$namespace" create secret generic "$release-user-secret" \
  --from-literal=fixture=user-owned >/dev/null

NAMESPACE="$namespace" ./pacman-uninstall.sh --helm-release "$release"
kubectl --context "$PACMAN_HELM_TEST_CONTEXT" get namespace "$namespace" -o name >/dev/null
kubectl --context "$PACMAN_HELM_TEST_CONTEXT" -n "$namespace" get pvc/"$release-data" secret/"$release-generated" secret/"$release-user-secret" -o name >/dev/null
status=$(helm --kube-context "$PACMAN_HELM_TEST_CONTEXT" list --all --namespace "$namespace" --output json \
  | python3 -c 'import json,sys; rows=json.load(sys.stdin); assert len(rows)==1; print(rows[0]["status"])')
[[ "$status" == uninstalled ]] || { echo 'Helm history was not retained' >&2; exit 1; }

NAMESPACE="$namespace" ./pacman-uninstall.sh --helm-release "$release" --delete-data
remaining_data=$(kubectl --context "$PACMAN_HELM_TEST_CONTEXT" -n "$namespace" \
  get pvc/"$release-data" secret/"$release-generated" --ignore-not-found -o name)
if [[ -n "$remaining_data" ]]; then
  echo 'Generated data resources remain after explicit cleanup' >&2; exit 1
fi
kubectl --context "$PACMAN_HELM_TEST_CONTEXT" -n "$namespace" get secret/"$release-user-secret" -o name >/dev/null
NAMESPACE="$namespace" ./pacman-uninstall.sh --helm-release "$release" --delete-data
kubectl --context "$PACMAN_HELM_TEST_CONTEXT" -n "$namespace" get secret/"$release-user-secret" -o name >/dev/null
echo 'PASS owned Helm release, retained data, explicit UID-bound cleanup and repeat'
