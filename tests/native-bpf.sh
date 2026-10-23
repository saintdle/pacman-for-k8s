#!/usr/bin/env bash
# Bounded native amd64 Cilium/Tetragon integration run for GitHub-hosted Linux.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER_TEMP_DIR="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
RUN_TAG="${NATIVE_BPF_RUN_TAG:-${GITHUB_RUN_ID:-local}-a${GITHUB_RUN_ATTEMPT:-1}-p$$}"
RUN_TAG="${RUN_TAG//[^a-zA-Z0-9-]/-}"
export NATIVE_BPF_RUN_TAG="$RUN_TAG"
CLUSTER_NAME="nbpf-${RUN_TAG}"
KIND_CONTEXT="kind-${CLUSTER_NAME}"
CILIUM_NAMESPACE="${CLUSTER_NAME}-eastwest"
TETRAGON_NAMESPACE="${CLUSTER_NAME}-tetragon"
EVIDENCE_DIR="${EVIDENCE_DIR:-${RUNNER_TEMP_DIR}/native-bpf-evidence-${RUN_TAG}}"
TOOL_DIR="${RUNNER_TEMP_DIR}/native-bpf-tools-${RUN_TAG}"
KUBECONFIG_FILE="${RUNNER_TEMP_DIR}/native-bpf-kubeconfig-${RUN_TAG}.yaml"
export KUBECONFIG="$KUBECONFIG_FILE"
STATE_DIR="${RUNNER_TEMP_DIR}/native-bpf-state-${RUN_TAG}"
CLUSTER_ATTEMPT_MARKER="$STATE_DIR/cluster-attempted"
PORT_FORWARD_PID_FILE="$STATE_DIR/port-forward.pid"
KUSTOMIZE_DIR_FILE="$STATE_DIR/kustomize-dir"
KUSTOMIZE_DIR=""
CILIUM_KUSTOMIZE_DIR=""
TETRAGON_KUSTOMIZE_DIR=""
PORT_FORWARD_PID=""
KIND_CONFIG="${RUNNER_TEMP_DIR}/native-bpf-kind-${RUN_TAG}.yaml"

MAIN_PHASE_TIMEOUT_SECONDS="${NATIVE_BPF_MAIN_PHASE_TIMEOUT_SECONDS:-1080}"
MAIN_PHASE_KILL_GRACE_SECONDS="${NATIVE_BPF_MAIN_PHASE_KILL_GRACE_SECONDS:-10}"
COMMAND_KILL_GRACE_SECONDS="${NATIVE_BPF_COMMAND_KILL_GRACE_SECONDS:-5}"
DOWNLOAD_TIMEOUT_SECONDS="${NATIVE_BPF_DOWNLOAD_TIMEOUT_SECONDS:-150}"
CURL_MAX_TIME_SECONDS="${NATIVE_BPF_CURL_MAX_TIME_SECONDS:-45}"
KIND_CREATE_TIMEOUT_SECONDS="${NATIVE_BPF_KIND_CREATE_TIMEOUT_SECONDS:-180}"
KIND_DELETE_TIMEOUT_SECONDS="${NATIVE_BPF_KIND_DELETE_TIMEOUT_SECONDS:-90}"
KIND_DELETE_KILL_GRACE_SECONDS="${NATIVE_BPF_KIND_DELETE_KILL_GRACE_SECONDS:-5}"
KUBECTL_TIMEOUT_SECONDS="${NATIVE_BPF_KUBECTL_TIMEOUT_SECONDS:-310}"
HELM_TIMEOUT_SECONDS="${NATIVE_BPF_HELM_TIMEOUT_SECONDS:-540}"
DIAGNOSTIC_TIMEOUT_SECONDS="${NATIVE_BPF_DIAGNOSTIC_TIMEOUT_SECONDS:-5}"
PORT_FORWARD_TIMEOUT_SECONDS="${NATIVE_BPF_PORT_FORWARD_TIMEOUT_SECONDS:-900}"
PORT_FORWARD_STOP_GRACE_SECONDS="${NATIVE_BPF_PORT_FORWARD_STOP_GRACE_SECONDS:-3}"

KIND_VERSION="v0.30.0"
KIND_NODE_IMAGE="kindest/node:v1.34.0@sha256:7416a61b42b1662ca6ca89f02028ac133a309a2a30ba309614e8ec94d976dc5a"
CILIUM_VERSION="1.19.4"
TETRAGON_VERSION="1.7.1"
KUBECTL_VERSION="v1.34.0"
HELM_VERSION="v3.17.3"
CILIUM_CLI_VERSION="v0.20.1"
POLICY_NAME="pacman-demo-process-and-file-observe"

mkdir -p "$EVIDENCE_DIR" "$TOOL_DIR"
chmod 700 "$EVIDENCE_DIR" "$TOOL_DIR"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
umask 077
export PATH="$TOOL_DIR:$PATH"

log() { printf '%s\n' "$*"; }
fail() { log "FAIL: $*" >&2; return 1; }

capture() {
  local name="$1"
  shift
  {
    timeout --signal=TERM --kill-after=1s "${DIAGNOSTIC_TIMEOUT_SECONDS}s" "$@" || true
  } >"$EVIDENCE_DIR/$name" 2>&1
}

run_with_timeout() {
  local seconds="$1"
  shift
  timeout --signal=TERM --kill-after="${COMMAND_KILL_GRACE_SECONDS}s" "${seconds}s" "$@"
}

download_file() {
  local url="$1" destination="$2"
  run_with_timeout "$DOWNLOAD_TIMEOUT_SECONDS" curl \
    --fail --location --silent --show-error --retry 2 --retry-max-time 100 \
    --connect-timeout 10 --max-time "$CURL_MAX_TIME_SECONDS" \
    "$url" -o "$destination"
}

run_main_phase() {
  timeout --signal=TERM --kill-after="${MAIN_PHASE_KILL_GRACE_SECONDS}s" \
    "${MAIN_PHASE_TIMEOUT_SECONDS}s" env "NATIVE_BPF_RUN_TAG=$RUN_TAG" "$@"
}

start_port_forward() {
  local attempt
  rm -f "$PORT_FORWARD_PID_FILE"
  # The literal inner script writes the child session leader PID before exec.
  # shellcheck disable=SC2016
  setsid bash -c 'pid_file=$1; duration=$2; shift 2; printf "%s\n" "$$" >"$pid_file"; exec timeout --foreground --signal=TERM --kill-after=2s "$duration" "$@"' \
    _ "$PORT_FORWARD_PID_FILE" "${PORT_FORWARD_TIMEOUT_SECONDS}s" kubectl \
    --kubeconfig "$KUBECONFIG_FILE" --context "$KIND_CONTEXT" \
    --namespace "$TETRAGON_NAMESPACE" port-forward "service/pacman-tetragon-demo" "${PROBE_PORT}:8080" \
    >"$EVIDENCE_DIR/tetragon-port-forward.txt" 2>&1 &
  for attempt in {1..20}; do
    [[ -s "$PORT_FORWARD_PID_FILE" ]] && return 0
    sleep 0.1
  done
  fail "port-forward process did not publish its isolated process-group id"
}

kubectl() {
  run_with_timeout "$KUBECTL_TIMEOUT_SECONDS" kubectl \
    --kubeconfig "$KUBECONFIG_FILE" --context "$KIND_CONTEXT" "$@"
}

helm() {
  run_with_timeout "$HELM_TIMEOUT_SECONDS" helm \
    --kubeconfig "$KUBECONFIG_FILE" --kube-context "$KIND_CONTEXT" "$@"
}

stop_port_forward() {
  local pid attempt
  [[ -f "$PORT_FORWARD_PID_FILE" ]] || return 0
  pid="$(cat "$PORT_FORWARD_PID_FILE")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  kill -TERM -- "-$pid" >/dev/null 2>&1 || true
  for ((attempt = 0; attempt < PORT_FORWARD_STOP_GRACE_SECONDS * 10; attempt++)); do
    kill -0 -- "-$pid" >/dev/null 2>&1 || break
    sleep 0.1
  done
  kill -KILL -- "-$pid" >/dev/null 2>&1 || true
  rm -f "$PORT_FORWARD_PID_FILE"
}

collect_diagnostics() {
  local node_name
  {
    printf 'uname: '
    uname -a || true
    printf 'host kernel: '
    uname -r || true
    printf 'host architecture: '
    uname -m || true
    printf 'host BTF: '
    if [[ -r /sys/kernel/btf/vmlinux ]]; then echo available; else echo unavailable; fi
    printf 'host cgroup controllers: '
    if [[ -r /sys/fs/cgroup/cgroup.controllers ]]; then cat /sys/fs/cgroup/cgroup.controllers; else echo unavailable; fi
    for tool in kind kubectl helm cilium docker; do
      if command -v "$tool" >/dev/null 2>&1; then
        printf '%s: ' "$tool"
        case "$tool" in
          kind) timeout --kill-after=1s 3s kind version 2>&1 || true ;;
          kubectl) timeout --kill-after=1s 3s kubectl version --client --output=yaml 2>&1 || true ;;
          helm) timeout --kill-after=1s 3s helm version --short 2>&1 || true ;;
          cilium) timeout --kill-after=1s 3s cilium version --client 2>&1 || true ;;
          docker) timeout --kill-after=1s 3s docker version --format '{{.Client.Version}} / {{.Server.Version}}' 2>&1 || true ;;
        esac
      fi
    done
  } >"$EVIDENCE_DIR/versions-kernel.txt" 2>&1

  if [[ -f "$KUBECONFIG_FILE" ]]; then
    capture nodes.txt kubectl get nodes -o wide
    capture pods-all-namespaces.txt kubectl get pods --all-namespaces -o wide
    capture events.txt kubectl get events --all-namespaces --sort-by=.lastTimestamp
    capture cilium-status.txt cilium status --context "$KIND_CONTEXT" --wait=false
    capture helm-releases.txt helm --kubeconfig "$KUBECONFIG_FILE" --kube-context "$KIND_CONTEXT" list --namespace kube-system
    capture cilium-pods.txt kubectl get pods --namespace kube-system --selector k8s-app=cilium -o wide
    capture cilium-agent-logs.txt kubectl logs --namespace kube-system --selector k8s-app=cilium --all-containers=true --prefix=true --tail=200 --max-log-requests=5
    capture cilium-operator-logs.txt kubectl logs --namespace kube-system --selector name=cilium-operator --all-containers=true --prefix=true --tail=200 --max-log-requests=3
    capture tetragon-pods.txt kubectl get pods --namespace kube-system --selector app.kubernetes.io/name=tetragon -o wide
    capture tetragon-agent-logs.txt kubectl logs --namespace kube-system --selector app.kubernetes.io/name=tetragon --container tetragon --prefix=true --tail=250 --max-log-requests=3
    capture tetragon-export-stdout.jsonl kubectl logs --namespace kube-system --selector app.kubernetes.io/name=tetragon --container export-stdout --prefix=true --tail=250 --max-log-requests=3
    capture tetragon-operator-logs.txt kubectl logs --namespace kube-system --selector app.kubernetes.io/name=tetragon-operator --all-containers=true --prefix=true --tail=200 --max-log-requests=3
    capture cilium-policy.txt kubectl get ciliumnetworkpolicies --all-namespaces -o wide
    capture tetragon-policy-resource.txt kubectl get tracingpolicy "$POLICY_NAME" -o yaml
    capture cilium-demo-resources.txt kubectl get deployments,services,endpointslices,ciliumnetworkpolicies --namespace "$CILIUM_NAMESPACE" -o wide
    capture tetragon-demo-resources.txt kubectl get deployments,services --namespace "$TETRAGON_NAMESPACE" -o wide
    node_name="${CLUSTER_NAME}-control-plane"
    capture kind-node-kernel.txt docker exec "$node_name" sh -c 'uname -a; readlink /proc/self/ns/cgroup; test -r /sys/kernel/btf/vmlinux && echo BTF=available; test -r /procHost/1/status && echo procHost=available'
  fi
}

cleanup() {
  local status=$?
  trap - EXIT
  set +e
  stop_port_forward
  if (( status != 0 )); then
    collect_diagnostics
  fi
  if [[ -f "$CLUSTER_ATTEMPT_MARKER" ]]; then
    if ! timeout --signal=TERM --kill-after="${KIND_DELETE_KILL_GRACE_SECONDS}s" \
      "${KIND_DELETE_TIMEOUT_SECONDS}s" env KUBECONFIG="$KUBECONFIG_FILE" \
      kind delete cluster --name "$CLUSTER_NAME" >"$EVIDENCE_DIR/cleanup.log" 2>&1; then
      log "FAIL: disposable Kind cluster cleanup failed; see bounded cleanup.log" >&2
      if (( status == 0 )); then status=1; fi
    fi
  fi
  if [[ -f "$KUSTOMIZE_DIR_FILE" ]]; then
    KUSTOMIZE_DIR="$(cat "$KUSTOMIZE_DIR_FILE")"
  fi
  if [[ -n "$KUSTOMIZE_DIR" ]]; then rm -rf "$KUSTOMIZE_DIR"; fi
  rm -rf "$STATE_DIR"
  rm -rf "$TOOL_DIR"
  rm -f "$KIND_CONFIG"
  rm -f "$KUBECONFIG_FILE"
  exit "$status"
}
preflight() {
  local kernel major minor
  [[ "$(uname -s)" == Linux ]] || fail "native BPF integration requires Linux" || return
  [[ "$(uname -m)" == x86_64 ]] || fail "native amd64 runner required; got $(uname -m)" || return
  kernel="$(uname -r)"
  if [[ "$kernel" =~ ^([0-9]+)\.([0-9]+) ]]; then
    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"
  else
    fail "cannot parse host kernel release: $kernel" || return
  fi
  if (( major < 5 || (major == 5 && minor < 10) )); then
    fail "Cilium/Tetragon integration requires Linux kernel 5.10 or newer; got $kernel" || return
  fi
  [[ -r /sys/kernel/btf/vmlinux ]] || fail "kernel BTF is required at /sys/kernel/btf/vmlinux" || return
  [[ -r /sys/fs/cgroup/cgroup.controllers ]] || fail "cgroup v2 is required for kube-proxy replacement" || return
  command -v curl >/dev/null || fail "curl is required" || return
  command -v sha256sum >/dev/null || fail "sha256sum is required" || return
  command -v git >/dev/null || fail "git is required to verify immutable source tags" || return
  command -v jq >/dev/null || fail "jq is required for bounded candidate identity checks" || return
  command -v python3 >/dev/null || fail "python3 is required for candidate identity checks" || return
  command -v timeout >/dev/null || fail "GNU timeout is required" || return
  command -v setsid >/dev/null || fail "setsid is required to contain the port-forward process group" || return
  command -v docker >/dev/null || fail "Docker is required by Kind" || return
  run_with_timeout 20 docker info >/dev/null || fail "Docker daemon is unavailable" || return
  {
    uname -a
    printf 'Architecture: %s\n' "$(uname -m)"
    printf 'BTF: available\n'
    printf 'cgroup controllers: %s\n' "$(cat /sys/fs/cgroup/cgroup.controllers)"
    run_with_timeout 10 docker version --format 'Docker client/server: {{.Client.Version}} / {{.Server.Version}}'
  } >"$EVIDENCE_DIR/preflight.txt" 2>&1
}

download_tools() {
  local kind_url kind_sum kubectl_base helm_base cilium_base
  kind_url="https://github.com/kubernetes-sigs/kind/releases/download/${KIND_VERSION}/kind-linux-amd64"
  kind_sum="${kind_url}.sha256sum"
  kubectl_base="https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
  helm_base="https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz"
  cilium_base="https://github.com/cilium/cilium-cli/releases/download/${CILIUM_CLI_VERSION}/cilium-linux-amd64.tar.gz"

  download_file "$kind_url" "$TOOL_DIR/kind-linux-amd64"
  download_file "$kind_sum" "$TOOL_DIR/kind-linux-amd64.sha256sum"
  (cd "$TOOL_DIR" && sha256sum --check kind-linux-amd64.sha256sum)
  install -m 0755 "$TOOL_DIR/kind-linux-amd64" "$TOOL_DIR/kind"

  download_file "$kubectl_base" "$TOOL_DIR/kubectl"
  download_file "${kubectl_base}.sha256" "$TOOL_DIR/kubectl.sha256"
  (cd "$TOOL_DIR" && printf '%s  kubectl\n' "$(cat kubectl.sha256)" | sha256sum --check)
  chmod 0755 "$TOOL_DIR/kubectl"

  download_file "$helm_base" "$TOOL_DIR/helm-${HELM_VERSION}-linux-amd64.tar.gz"
  download_file "${helm_base}.sha256sum" "$TOOL_DIR/helm-${HELM_VERSION}-linux-amd64.tar.gz.sha256sum"
  (cd "$TOOL_DIR" && sha256sum --check "helm-${HELM_VERSION}-linux-amd64.tar.gz.sha256sum")
  tar -xzf "$TOOL_DIR/helm-${HELM_VERSION}-linux-amd64.tar.gz" -C "$TOOL_DIR" linux-amd64/helm
  install -m 0755 "$TOOL_DIR/linux-amd64/helm" "$TOOL_DIR/helm"

  download_file "$cilium_base" "$TOOL_DIR/cilium-linux-amd64.tar.gz"
  download_file "${cilium_base}.sha256sum" "$TOOL_DIR/cilium-linux-amd64.tar.gz.sha256sum"
  (cd "$TOOL_DIR" && sha256sum --check cilium-linux-amd64.tar.gz.sha256sum)
  tar -xzf "$TOOL_DIR/cilium-linux-amd64.tar.gz" -C "$TOOL_DIR" cilium
  chmod 0755 "$TOOL_DIR/cilium"
  export PATH="$TOOL_DIR:$PATH"
  {
    kind version
    kubectl version --client --output=yaml
    helm version --short
    cilium version --client
  } >"$EVIDENCE_DIR/tool-versions.txt" 2>&1
  grep -Fq "$KIND_VERSION" "$EVIDENCE_DIR/tool-versions.txt" || fail "unexpected Kind version"
  grep -Fq "$KUBECTL_VERSION" "$EVIDENCE_DIR/tool-versions.txt" || fail "unexpected kubectl version"
  grep -Fq "$HELM_VERSION" "$EVIDENCE_DIR/tool-versions.txt" || fail "unexpected Helm version"
  grep -Fq "$CILIUM_CLI_VERSION" "$EVIDENCE_DIR/tool-versions.txt" || fail "unexpected Cilium CLI version"
}

download_chart() {
  local name="$1" url="$2" digest="$3" destination="$4"
  download_file "$url" "$destination"
  printf '%s  %s\n' "$digest" "$destination" | sha256sum --check
}

write_kind_config() {
  cat >"$KIND_CONFIG" <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
networking:
  disableDefaultCNI: true
  kubeProxyMode: none
nodes:
  - role: control-plane
    image: ${KIND_NODE_IMAGE}
    extraMounts:
      - hostPath: /proc
        containerPath: /procHost
        readOnly: true
EOF
}

wait_for_api() {
  local attempt
  for attempt in {1..60}; do
    if kubectl get --raw=/readyz >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  fail "Kubernetes API did not become ready within 120 seconds"
}

create_kind_cluster() {
  touch "$CLUSTER_ATTEMPT_MARKER"
  run_with_timeout "$KIND_CREATE_TIMEOUT_SECONDS" kind create cluster \
    --name "$CLUSTER_NAME" --image "$KIND_NODE_IMAGE" \
    --config "$KIND_CONFIG" --kubeconfig "$KUBECONFIG_FILE"
}

assert_node_prerequisites() {
  local node_name host_cgroup node_cgroup node_arch
  node_name="${CLUSTER_NAME}-control-plane"
  host_cgroup="$(readlink /proc/self/ns/cgroup)"
  node_cgroup="$(docker exec "$node_name" readlink /proc/self/ns/cgroup)"
  [[ "$host_cgroup" != "$node_cgroup" ]] || fail "Kind node and host share a cgroup namespace; Cilium socket load-balancing prerequisites are unsupported" || return
  run_with_timeout 20 docker exec "$node_name" test -r /sys/kernel/btf/vmlinux || fail "Kind node cannot access host kernel BTF" || return
  run_with_timeout 20 docker exec "$node_name" test -r /procHost/1/status || fail "Kind node cannot access the mounted host /proc at /procHost" || return
  node_arch="$(kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.architecture}')"
  [[ "$node_arch" == amd64 ]] || fail "Kind node architecture must be amd64; got $node_arch" || return
  {
    printf 'host cgroup namespace: %s\n' "$host_cgroup"
    printf 'Kind node cgroup namespace: %s\n' "$node_cgroup"
    printf 'Kind node architecture: %s\n' "$node_arch"
    run_with_timeout 20 docker exec "$node_name" uname -a
    run_with_timeout 20 docker exec "$node_name" cat /procHost/version
    run_with_timeout 20 docker exec "$node_name" test -r /sys/kernel/btf/vmlinux && echo 'Kind node BTF: available'
    run_with_timeout 20 docker exec "$node_name" test -r /procHost/1/status && echo 'Kind node host /proc: available'
  } >"$EVIDENCE_DIR/node-prerequisites.txt" 2>&1
}

download_and_install_cilium() {
  local chart="$TOOL_DIR/cilium-${CILIUM_VERSION}.tgz"
  download_chart "cilium" "https://helm.cilium.io/cilium-${CILIUM_VERSION}.tgz" \
    "c6a788e6bd74598a235f7987930860f554c2dd8048154d1aba8a532bcde90cd6" "$chart"
  helm upgrade --install cilium "$chart" \
    --namespace kube-system \
    --set kubeProxyReplacement=true \
    --set "k8sServiceHost=${CLUSTER_NAME}-control-plane" \
    --set k8sServicePort=6443 \
    --set ipam.mode=kubernetes \
    --set operator.replicas=1 \
    --wait --timeout 8m
  run_with_timeout 120 cilium status --context "$KIND_CONTEXT" --wait
  kubectl wait --for=condition=Ready node --all --timeout=300s
  local node_arch
  node_arch="$(kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.architecture}')"
  [[ "$node_arch" == amd64 ]] || fail "ready Kind node architecture must be amd64; got $node_arch"
  printf 'Kind node architecture: %s\n' "$node_arch" >"$EVIDENCE_DIR/nodes-ready.txt"
  kubectl get nodes -o wide >>"$EVIDENCE_DIR/nodes-ready.txt"
  cilium version --client >"$EVIDENCE_DIR/cilium-version.txt" 2>&1
}

write_workload_kustomization() {
  local image_repository image_digest
  KUSTOMIZE_DIR="$(mktemp -d "$RUNNER_TEMP_DIR/native-bpf-kustomize-${RUN_TAG}.XXXXXXXX")"
  printf '%s\n' "$KUSTOMIZE_DIR" >"$KUSTOMIZE_DIR_FILE"
  CILIUM_KUSTOMIZE_DIR="$KUSTOMIZE_DIR/cilium"
  TETRAGON_KUSTOMIZE_DIR="$KUSTOMIZE_DIR/tetragon"
  mkdir -p "$CILIUM_KUSTOMIZE_DIR" "$TETRAGON_KUSTOMIZE_DIR"
  image_repository="$(jq -r '.candidate.app.image_repository' "$EVIDENCE_DIR/candidate-identity.json")"
  image_digest="$(jq -r '.candidate.app.image_manifest_list_digest' "$EVIDENCE_DIR/candidate-identity.json")"
  cp "$ROOT/cilium-demos/microservices-east-west/workloads.yaml" "$CILIUM_KUSTOMIZE_DIR/workloads.yaml"
  cp "$ROOT/cilium-demos/tetragon-enforcement/tetragon-demo.yaml" "$TETRAGON_KUSTOMIZE_DIR/tetragon-demo.yaml"
  cp "$ROOT/cilium-demos/tetragon-enforcement/tracingpolicy.yaml" "$TETRAGON_KUSTOMIZE_DIR/tracingpolicy.yaml"
  python3 "$ROOT/tests/native_bpf_candidate.py" rewrite-version \
    --identity "$EVIDENCE_DIR/candidate-identity.json" --manifest "$CILIUM_KUSTOMIZE_DIR/workloads.yaml"
  python3 "$ROOT/tests/native_bpf_candidate.py" rewrite-version \
    --identity "$EVIDENCE_DIR/candidate-identity.json" --manifest "$TETRAGON_KUSTOMIZE_DIR/tetragon-demo.yaml"
  cat >"$CILIUM_KUSTOMIZE_DIR/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: ${CILIUM_NAMESPACE}
labels:
  - pairs:
      app.kubernetes.io/part-of: pacman-cilium-demo
    includeSelectors: false
resources:
  - workloads.yaml
images:
  - name: docker.io/saintdle/pacman
    newName: ${image_repository}
    digest: ${image_digest}
EOF
  cat >"$TETRAGON_KUSTOMIZE_DIR/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - tetragon-demo.yaml
  - tracingpolicy.yaml
images:
  - name: docker.io/saintdle/pacman
    newName: ${image_repository}
    digest: ${image_digest}
EOF
}

VERSION_PROBE='fetch("http://127.0.0.1:8080/version").then(async response => { if (!response.ok) throw new Error(`/version returned ${response.status}`); console.log(JSON.stringify(await response.json())); }).catch(error => { console.error(error.message); process.exitCode = 1; });'

verify_candidate_pods() {
  local namespace="$1" name="$2"
  local pods_file="$EVIDENCE_DIR/${name}-pods.json"
  local runtime_file="$EVIDENCE_DIR/${name}-runtime.jsonl" pod_rows pod role node runtime_file_one
  local image_reference kind_nodes node_info_file runtime_version cri_inspect_file cri_file
  image_reference="$(jq -er '.candidate.image_reference' "$EVIDENCE_DIR/candidate-identity.json")"
  kind_nodes="$(run_with_timeout 30 kind get nodes --name "$CLUSTER_NAME")"
  [[ -n "$kind_nodes" ]] || fail "owned Kind cluster has no nodes"
  kubectl --namespace "$namespace" get pods \
    --selector app.kubernetes.io/part-of=pacman-cilium-demo --output=json >"$pods_file"
  pod_rows="$(jq -r '.items[] | select(.status.phase == "Running" and .metadata.deletionTimestamp == null) | [.metadata.name, .metadata.labels["app.kubernetes.io/component"], .spec.nodeName] | @tsv' "$pods_file")"
  [[ -n "$pod_rows" ]] || fail "$namespace has no running Pac-Man candidate pods"
  : >"$runtime_file"
  while IFS=$'\t' read -r pod role node; do
    [[ -n "$pod" && -n "$role" && -n "$node" ]] || fail "$namespace returned a candidate pod without a name, role, or assigned node"
    grep -Fxq -- "$node" <<<"$kind_nodes" || fail "$namespace pod $pod is not on a node owned by Kind cluster $CLUSTER_NAME"
    runtime_file_one="$EVIDENCE_DIR/${name}-${pod}-version.json"
    kubectl --namespace "$namespace" exec "$pod" --container pacman -- node -e "$VERSION_PROBE" >"$runtime_file_one"
    node_info_file="$EVIDENCE_DIR/${name}-${node}-node.json"
    cri_inspect_file="$EVIDENCE_DIR/${name}-${node}-cri-inspect.json"
    cri_file="$EVIDENCE_DIR/${name}-${node}-cri.json"
    if [[ ! -s "$cri_file" ]]; then
      kubectl get node "$node" --output=json >"$node_info_file"
      runtime_version="$(jq -er '.status.nodeInfo.containerRuntimeVersion | strings | select(length > 0)' "$node_info_file")"
      [[ "$runtime_version" == containerd://* && -n "${runtime_version#containerd://}" \
        && "${runtime_version#containerd://}" != *[[:space:]]* ]] \
        || fail "$node does not report a valid containerd runtime version"
      run_with_timeout 30 docker exec "$node" crictl inspecti "$image_reference" >"$cri_inspect_file"
      jq -e '.status.id and (.status.repoDigests | type == "array" and length > 0)' "$cri_inspect_file" >/dev/null \
        || fail "$node CRI did not report the candidate image config ID and repository digests"
      jq -cn --arg node "$node" --arg container_runtime_version "$runtime_version" \
        --arg image_reference "$image_reference" --slurpfile inspect "$cri_inspect_file" \
        '{node:$node,container_runtime_version:$container_runtime_version,image_reference:$image_reference,inspect:$inspect[0]}' \
        >"$cri_file"
    fi
    jq -cn --arg pod "$pod" --slurpfile version "$runtime_file_one" --slurpfile cri "$cri_file" \
      '{pod:$pod,version:$version[0],cri:$cri[0]}' >>"$runtime_file"
  done <<<"$pod_rows"
  python3 "$ROOT/tests/native_bpf_candidate.py" verify-pods \
    --identity "$EVIDENCE_DIR/candidate-identity.json" \
    --pods "$pods_file" --runtimes "$runtime_file" \
    --output "$EVIDENCE_DIR/${name}-candidate-identity.json"
}

HTTP_PROBE="$(cat <<'NODE'
(async () => {
  const name = `native-bpf-${process.argv[1]}`;
  const form = (values) => new URLSearchParams(values);
  const request = async (url, options = {}) => {
    const response = await fetch(url, { ...options, signal: AbortSignal.timeout(5000) });
    const body = await response.text();
    return { status: response.status, body };
  };
  const userIdFrom = (body) => {
    let value;
    try { value = JSON.parse(body); } catch { value = body.trim(); }
    if (typeof value === 'string' || typeof value === 'number') return String(value);
    if (value && typeof value === 'object') {
      for (const key of ['userId', '_id', 'id']) {
        if (value[key] !== undefined && value[key] !== null) return String(value[key]);
      }
    }
    throw new Error('user session response did not contain an ID');
  };
  const scoreVersion = await request('http://pacman-score:8080/version');
  const [internalScoreRead, publicScoreRead, publicScoreWrite, internalUserSession, publicUserSession] = await Promise.all([
    request('http://pacman-score:8080/internal/score/read'),
    request('http://pacman-web:8080/highscores/list'),
    request('http://pacman-web:8080/highscores', { method: 'POST', body: form({ name, score: '500', level: '1' }) }),
    request(`http://pacman-user:8080/internal/user/session?name=${encodeURIComponent(name)}`),
    request(`http://pacman-web:8080/user/id?name=${encodeURIComponent(name)}`),
  ]);
  const statsPayload = (userId) => form({ userId, name, score: '77', level: '1', lives: '3', elapsedTime: '42' });
  const internalUserId = userIdFrom(internalUserSession.body);
  const publicUserId = userIdFrom(publicUserSession.body);
  const [internalUserStatsWrite, publicUserStatsWrite] = await Promise.all([
    request('http://pacman-user:8080/internal/user/stats', { method: 'POST', body: statsPayload(internalUserId) }),
    request('http://pacman-web:8080/user/stats', { method: 'POST', body: statsPayload(publicUserId) }),
  ]);
  process.stdout.write(JSON.stringify({
    score_version: scoreVersion.status,
    routes: {
      internal_score_read: internalScoreRead.status,
      public_score_read: publicScoreRead.status,
      public_score_write: publicScoreWrite.status,
      internal_user_session: internalUserSession.status,
      public_user_session: publicUserSession.status,
      internal_user_stats_write: internalUserStatsWrite.status,
      public_user_stats_write: publicUserStatsWrite.status,
    },
  }) + '\n');
})().catch((error) => {
  console.error(`HTTP probe failed: ${error.message}`);
  process.exit(1);
});
NODE
)"

run_http_probe() {
  local stage="$1" result="$EVIDENCE_DIR/http-${1}.json" attempt
  local error_file="$EVIDENCE_DIR/http-${1}-last-error.txt"
  local check_file="$EVIDENCE_DIR/http-${1}-last-check.txt"
  local attempt_limit=30
  for attempt in $(seq 1 "$attempt_limit"); do
    if kubectl --namespace "$CILIUM_NAMESPACE" exec "$WEB_POD" -- \
      node -e "$HTTP_PROBE" "$RUN_TAG" >"$result" 2>"$error_file" && \
      python3 "$ROOT/tests/native_bpf_assert.py" http "$stage" "$result" >"$check_file" 2>&1; then
      cat "$check_file"
      return 0
    fi
    sleep 2
  done
  cat "$check_file" "$error_file" >&2 2>/dev/null || true
  fail "$stage HTTP assertion did not converge after $attempt_limit bounded attempts"
}

run_cilium_integration() {
  local attempt
  write_workload_kustomization
  kubectl create namespace "$CILIUM_NAMESPACE"
  # The temporary Kustomization preserves the checked-in namespace and cluster RBAC transformations.
  kubectl apply -k "$CILIUM_KUSTOMIZE_DIR"
  for deployment in pacman-web pacman-score pacman-user; do
    kubectl --namespace "$CILIUM_NAMESPACE" rollout status "deployment/$deployment" --timeout=240s
  done
  verify_candidate_pods "$CILIUM_NAMESPACE" cilium
  WEB_POD="$(kubectl --namespace "$CILIUM_NAMESPACE" get pods \
    --selector app.kubernetes.io/name=pacman,app.kubernetes.io/component=web \
    --output=jsonpath='{.items[0].metadata.name}')"
  [[ -n "$WEB_POD" ]] || fail "no ready web-role pod was found"
  kubectl --namespace "$CILIUM_NAMESPACE" get pod "$WEB_POD" \
    --output=jsonpath='{.metadata.labels.app\.kubernetes\.io/component}' | grep -qx web || fail "selected source pod is not the web role"
  log "Testing Cilium policy baseline from pod $CILIUM_NAMESPACE/$WEB_POD"
  run_http_probe baseline

  kubectl --namespace "$CILIUM_NAMESPACE" apply -f "$ROOT/cilium-demos/microservices-east-west/cilium-network-policy.yaml"
  log "Waiting for actual Cilium L7 datapath behavior from the web-role pod"
  run_http_probe active

  kubectl --namespace "$CILIUM_NAMESPACE" delete -f "$ROOT/cilium-demos/microservices-east-west/cilium-network-policy.yaml" \
    --wait=true --timeout=180s
  run_http_probe recovery
  {
    printf 'baseline: '
    cat "$EVIDENCE_DIR/http-baseline.json"
    printf 'policy-active: '
    cat "$EVIDENCE_DIR/http-active.json"
    printf 'recovery: '
    cat "$EVIDENCE_DIR/http-recovery.json"
  } >"$EVIDENCE_DIR/cilium-http-results.txt"
}

download_and_install_tetragon() {
  local chart="$TOOL_DIR/tetragon-${TETRAGON_VERSION}.tgz"
  download_chart "tetragon" "https://helm.cilium.io/tetragon-${TETRAGON_VERSION}.tgz" \
    "79ec0f7d35b6fcad99ca317348deac136c00b674430c28be17aa818d2015c632" "$chart"
  helm upgrade --install tetragon "$chart" \
    --namespace kube-system \
    --set tetragon.hostProcPath=/procHost \
    --set export.mode=stdout \
    --wait --timeout 8m
  kubectl --namespace kube-system rollout status daemonset/tetragon --timeout=240s
  kubectl --namespace kube-system rollout status deployment/tetragon-operator --timeout=240s
  kubectl --namespace kube-system get daemonsets,deployments -o wide >"$EVIDENCE_DIR/tetragon-installed.txt"
  helm list --namespace kube-system --output json >"$EVIDENCE_DIR/helm-releases.json"
}

wait_for_tetragon_policy() {
  local pod="$1" attempt
  local status_file="$EVIDENCE_DIR/tetragon-policy-status.json"
  for attempt in $(seq 1 45); do
    if kubectl --namespace kube-system exec "$pod" --container tetragon -- tetra tracingpolicy list --output json >"$status_file" 2>"$EVIDENCE_DIR/tetra-policy-error.txt" && \
      python3 "$ROOT/tests/native_bpf_assert.py" policy "$status_file" --name "$POLICY_NAME" >"$EVIDENCE_DIR/tetra-policy-check.txt" 2>&1; then
      cat "$EVIDENCE_DIR/tetra-policy-check.txt"
      return 0
    fi
    sleep 2
  done
  cat "$EVIDENCE_DIR/tetra-policy-check.txt" "$EVIDENCE_DIR/tetra-policy-error.txt" >&2 2>/dev/null || true
  fail "Tetragon did not report policy $POLICY_NAME enabled with loaded sensors"
}

check_demo_probe() {
  local path="$1" status
  status="$(curl --noproxy '*' --silent --show-error --connect-timeout 3 --max-time 10 \
    --output /dev/null --write-out '%{http_code}' -X POST \
    -H 'x-demo-token: pacman-demo' "http://127.0.0.1:${PROBE_PORT}${path}")" || \
    fail "authenticated Tetragon probe request failed: $path"
  [[ "$status" == 200 ]] || fail "$path expected HTTP 200; observed $status"
  printf '%s=%s\n' "$path" "$status" >>"$EVIDENCE_DIR/tetragon-probe-results.txt"
}

run_tetragon_integration() {
  local tetra_pod attempt event_start
  kubectl create namespace "$TETRAGON_NAMESPACE"
  kubectl --namespace "$TETRAGON_NAMESPACE" apply -k "$TETRAGON_KUSTOMIZE_DIR"
  kubectl --namespace "$TETRAGON_NAMESPACE" rollout status deployment/pacman-tetragon-demo --timeout=240s
  verify_candidate_pods "$TETRAGON_NAMESPACE" tetragon
  kubectl --namespace "$TETRAGON_NAMESPACE" get pod \
    --selector app.kubernetes.io/name=pacman,app.kubernetes.io/component=web -o wide \
    >"$EVIDENCE_DIR/tetragon-demo-pods.txt"
  tetra_pod="$(kubectl --namespace kube-system get pods \
    --selector app.kubernetes.io/name=tetragon \
    --output=jsonpath='{.items[0].metadata.name}')"
  [[ -n "$tetra_pod" ]] || fail "no Tetragon agent pod was found"
  wait_for_tetragon_policy "$tetra_pod"

  PROBE_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
  start_port_forward
  PORT_FORWARD_PID="$(cat "$PORT_FORWARD_PID_FILE")"
  for attempt in {1..30}; do
    if curl --noproxy '*' --silent --show-error --connect-timeout 2 --max-time 4 \
      --output /dev/null "http://127.0.0.1:${PROBE_PORT}/version"; then
      break
    fi
    kill -0 "$PORT_FORWARD_PID" 2>/dev/null || fail "Tetragon demo port-forward exited before becoming ready"
    sleep 1
  done
  curl --noproxy '*' --silent --show-error --connect-timeout 2 --max-time 4 \
    --output /dev/null "http://127.0.0.1:${PROBE_PORT}/version" || fail "Tetragon demo service did not become reachable"

  : >"$EVIDENCE_DIR/tetragon-probe-results.txt"
  event_start="$(date -u --date='5 seconds ago' +%Y-%m-%dT%H:%M:%SZ)"
  check_demo_probe /demo/incidents/file-probe
  check_demo_probe /demo/incidents/exec-probe

  attempt=0
  while (( attempt < 30 )); do
    attempt=$((attempt + 1))
    kubectl --namespace kube-system logs "$tetra_pod" --container export-stdout \
      --since-time="$event_start" --tail=1000 >"$EVIDENCE_DIR/tetragon-events.jsonl" 2>"$EVIDENCE_DIR/tetragon-events-error.txt" || true
    if python3 "$ROOT/tests/native_bpf_assert.py" events "$EVIDENCE_DIR/tetragon-events.jsonl" \
      --namespace "$TETRAGON_NAMESPACE" --pod "$(kubectl --namespace "$TETRAGON_NAMESPACE" get pods \
        --selector app.kubernetes.io/name=pacman,app.kubernetes.io/component=web \
        --output=jsonpath='{.items[0].metadata.name}')" --policy "$POLICY_NAME" \
        >"$EVIDENCE_DIR/tetragon-events-check.txt" 2>&1; then
      cat "$EVIDENCE_DIR/tetragon-events-check.txt"
      return 0
    fi
    sleep 2
  done
  cat "$EVIDENCE_DIR/tetragon-probe-results.txt" "$EVIDENCE_DIR/tetragon-events-check.txt" \
    "$EVIDENCE_DIR/tetragon-events-error.txt" >&2 2>/dev/null || true
  fail "bounded Tetragon stdout did not contain both correlated probe events"
}

main() {
  log "Native BPF integration: $CLUSTER_NAME"
  preflight
  run_with_timeout "${NATIVE_BPF_CANDIDATE_TIMEOUT_SECONDS:-240}" python3 "$ROOT/tests/native_bpf_candidate.py" \
    prepare --root "$ROOT" --evidence-dir "$EVIDENCE_DIR"
  download_tools
  export PATH="$TOOL_DIR:$PATH"
  write_kind_config

  # This kubeconfig is unique to this run; the caller's existing kubeconfig is never read or changed.
  create_kind_cluster
  chmod 600 "$KUBECONFIG_FILE"
  wait_for_api
  assert_node_prerequisites

  download_and_install_cilium
  run_cilium_integration
  download_and_install_tetragon
  run_tetragon_integration
  log "PASS: Cilium allow/deny/recovery and observe-only Tetragon probe assertions completed"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  if [[ "${NATIVE_BPF_PHASE:-}" == worker ]]; then
    main "$@"
  else
    trap cleanup EXIT
    phase_status=0
    if run_main_phase env NATIVE_BPF_PHASE=worker bash "$0" "$@"; then
      phase_status=0
    else
      phase_status=$?
    fi
    exit "$phase_status"
  fi
fi
