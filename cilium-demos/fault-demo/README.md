# Manual fault and troubleshooting demos

Run these scenarios one at a time in a disposable kind cluster. Use Helm to
change the workload, inspect it with Hubble, Prometheus, or Tetragon, then
restore the baseline. There is no scenario runner.

| Scenario | What to look for |
|---|---|
| [1. Healthy baseline](#1-healthy-baseline) | Working gameplay, score reads, HTTP flows, and scrape targets |
| [2. Request-rate spike](#2-request-rate-spike) | More requests from simulated users |
| [3. Score backend latency](#3-score-backend-latency) | Slow backend requests without injected errors |
| [4. Score backend 5xx](#4-score-backend-5xx) | Backend errors visible in both metrics and flows |
| [5. Score writes denied](#5-score-writes-denied) | Writes blocked by policy while reads still work |
| [6. Missing L7 visibility](#6-missing-l7-visibility) | Traffic continues without HTTP flow details |
| [7. Hubble HTTP exporter disabled](#7-hubble-http-exporter-disabled) | HTTP metrics disappear but HTTP flows remain |
| [8. Metrics scrape failure](#8-metrics-scrape-failure) | App targets disappear while the app stays healthy |
| [9. Synthetic file read: observe, then enforce](#9-synthetic-file-read-observe-then-enforce) | A fixed synthetic file read is observed, then denied |
| [10. Child execution: observe, then enforce](#10-child-execution-observe-then-enforce) | A fixed helper runs, then is killed without killing the frontend |

## Prerequisites

Install Git, Docker, kind, kubectl, Helm, the Cilium and Hubble CLIs, curl, and
OpenSSL. Run commands from the root of this repository, in Bash.

The released app image `0.7.16` contains the gated fault and synthetic
security-probe runtime. The published Pac-Man chart `0.7.20` from the
[versioned chart release](https://github.com/saintdle/helm-charts/releases/tag/pacman-0.7.20)
exposes its opt-in controls. The commands below build a local Kind image from
the exact app release source and pull the versioned chart. This local rebuild is for the walkthrough's
`pullPolicy: Never`; it does not reproduce the published image digest or
attestations. The stable public chart repository remains
<https://saintdle.github.io/helm-charts>.

The Tetragon enforcement scenarios also need a supported native Linux kernel
with BTF and `CONFIG_BPF_KPROBE_OVERRIDE=y`. kind uses the container host's
kernel (the Linux VM kernel on Docker Desktop). Emulated containers do not
validate another architecture's BPF support. Stop if the policy cannot load.

The public [native runtime run](https://github.com/saintdle/pacman-for-k8s/actions/runs/40221882676)
verified Cilium allow/deny/policy-removal recovery and the loaded observe-only
Tetragon policy with correlated file and exec events. It did not run this
ten-scenario walkthrough. The manual scenarios, Tetragon enforcement, and
external LoadBalancer/provider behavior remain unvalidated by that evidence.

## Prepare the lab

Use a new cluster name; do not point these commands at an existing shared
cluster. The existing setup helper changes the current kubectl context.

```bash
export CLUSTER_NAME=pacman-manual
export CONTEXT="kind-$CLUSTER_NAME"
export HELM_KUBECONTEXT="$CONTEXT"
export NAMESPACE=pacman-fault-demo
export DEMO_DIR="$PWD/cilium-demos/fault-demo"
LAB_DIR="$(mktemp -d)"
export LAB_DIR
export APP_REVISION=13f04d6e6aea62f1d32d57d90e4eff786083c02e
export APP_IMAGE=pacman-manual-demo:13f04d6e
export CHART_VERSION=0.7.20

git clone https://github.com/saintdle/pacman.git "$LAB_DIR/app"
git -C "$LAB_DIR/app" checkout --detach "$APP_REVISION"
helm repo add pacman-charts https://saintdle.github.io/helm-charts
helm repo update pacman-charts
helm pull pacman-charts/pacman --version "$CHART_VERSION" --untar --untardir "$LAB_DIR"
export CHART="$LAB_DIR/pacman"

docker build -f "$LAB_DIR/app/docker/Dockerfile" \
  --build-arg COMMIT_SHA="$APP_REVISION" -t "$APP_IMAGE" "$LAB_DIR/app"
CILIUM_VERSION=1.19.4 ./cilium-demos/scripts/setup-single-kind.sh
kind load docker-image "$APP_IMAGE" --name "$CLUSTER_NAME"
```

The image is built for the local Docker architecture. Its tag matches
[baseline.yaml](values/baseline.yaml), which uses `pullPolicy: Never` for this
kind-only lab.

Install Prometheus before enabling ServiceMonitors. Keep the component
versions below together when reproducing the examples.

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  --version 91.5.1 --namespace monitoring --create-namespace \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
  --set grafana.enabled=false --set alertmanager.enabled=false \
  --wait --timeout 10m

helm upgrade cilium cilium/cilium --version 1.19.4 --namespace kube-system \
  --reuse-values -f "$DEMO_DIR/values/cilium.yaml" --wait --timeout 5m
helm upgrade tetragon cilium/tetragon --version 1.7.1 --namespace kube-system \
  --reset-values --set tetragon.enablePolicyFilter=true --wait --timeout 5m

kubectl --context "$CONTEXT" create namespace "$NAMESPACE"
DEMO_TOKEN="$(openssl rand -hex 24)"
export DEMO_TOKEN
kubectl --context "$CONTEXT" -n "$NAMESPACE" create secret generic pacman-demo-token \
  --from-literal="token=$DEMO_TOKEN"
```

Keep `DEMO_TOKEN` in this shell for the two manual security probes. Do not
commit it or include it in screenshots. The chart references the Secret;
the YAML examples contain no token.

## 1. Healthy baseline

**Run:**

```bash
helm upgrade --install pacman-fault "$CHART" -n "$NAMESPACE" \
  --reset-values -f "$DEMO_DIR/values/baseline.yaml" --wait --timeout 5m
kubectl --context "$CONTEXT" -n "$NAMESPACE" get pods
```

Start each port-forward below in a separate terminal, using the same context
and namespace. Leave them in the foreground; stop them with Ctrl-C. Restart
a forward if its selected pod is replaced during a scenario.
If you changed the lab name or namespace above, replace the matching literals
in these three commands too.

```bash
kubectl --context kind-pacman-manual -n pacman-fault-demo port-forward svc/pacman-fault 8080:80
```

```bash
kubectl --context kind-pacman-manual -n monitoring port-forward svc/prometheus-operated 9090:9090
```

```bash
kubectl --context kind-pacman-manual -n kube-system port-forward svc/hubble-relay 4245:80
```

**Observe:** open Pac-Man at <http://localhost:8080> and Prometheus at
<http://localhost:9090>. Allow two minutes of simulator traffic before
comparing two-minute metric rates. In Prometheus, check that the
`pacman-fault`, `pacman-fault-score`, and `pacman-fault-user` targets are UP.

```bash
curl -fsS http://localhost:8080/healthz
curl -fsS http://localhost:8080/readyz
curl -fsS http://localhost:8080/highscores/list
hubble observe --server localhost:4245 --namespace "$NAMESPACE" --protocol http --since 30s
```

Record the following PromQL results for comparison:

```promql
sum(rate(pacman_http_requests_total{namespace="pacman-fault-demo",service="pacman-fault",role="web",route!="/metrics"}[2m]))
```

```promql
sum(rate(pacman_upstream_latency_ms_sum{namespace="pacman-fault-demo",service="pacman-fault",target="pacman-fault-score",method="GET"}[2m]))
/
sum(rate(pacman_upstream_latency_ms_count{namespace="pacman-fault-demo",service="pacman-fault",target="pacman-fault-score",method="GET"}[2m]))
```

**Expected:** the requests succeed, HTTP score traffic is present, and both
queries have samples. The second query is mean upstream latency in
milliseconds. Do not treat an empty query as a healthy zero.

**Reset:** this is the baseline. Use [Reset](#reset) after each later scenario.
Every Pac-Man upgrade below uses `--reset-values` plus the baseline, so faults
do not accumulate through Helm's saved values.

## 2. Request-rate spike

**Run:**

```bash
helm upgrade pacman-fault "$CHART" -n "$NAMESPACE" --reset-values \
  -f "$DEMO_DIR/values/baseline.yaml" \
  --set simulator.users=20 --set simulator.minThinkMs=100 --set simulator.maxThinkMs=300 \
  --wait --timeout 5m
```

**Observe:** wait two minutes, repeat the baseline request-rate query, and
inspect traffic:

```bash
hubble observe --server localhost:4245 --namespace "$NAMESPACE" --protocol http --since 30s
```

**Expected:** request rate is at least 1.5 times the recorded baseline.
If it is not, inspect simulator logs and resource pressure rather than
assuming the values change proves a rate increase.

**Reset:** follow [Reset](#reset); the rate should return toward the baseline.

## 3. Score backend latency

**Run:**

```bash
helm upgrade pacman-fault "$CHART" -n "$NAMESPACE" --reset-values \
  -f "$DEMO_DIR/values/baseline.yaml" -f "$DEMO_DIR/values/score-backend-latency.yaml" \
  --wait --timeout 5m
```

**Observe:** repeat the baseline latency query after two minutes and inspect
score requests:

```bash
hubble observe --server localhost:4245 --namespace "$NAMESPACE" \
  --protocol http --http-path '/internal/score/' --since 30s
```

**Expected:** mean score-read latency is at least 500 ms and five times its
healthy value. The simulator requests 800-1500 ms delays on 80% of its score
operations; ordinary manual requests do not carry its fault headers.

The schedule runs for 300 seconds from simulator startup. If it expired
before observation, restart `deployment/pacman-fault-simulator` with
`kubectl rollout restart` using this lab's context and namespace, then repeat.

**Reset:** follow [Reset](#reset); latency should return toward its healthy value.

## 4. Score backend 5xx

**Run:**

```bash
helm upgrade pacman-fault "$CHART" -n "$NAMESPACE" --reset-values \
  -f "$DEMO_DIR/values/baseline.yaml" -f "$DEMO_DIR/values/score-backend-5xx.yaml" \
  --wait --timeout 5m
```

**Observe:** query Prometheus during the 300-second fault schedule:

```promql
sum(rate(pacman_upstream_requests_total{namespace="pacman-fault-demo",service="pacman-fault",target="pacman-fault-score",status=~"5.."}[2m]))
```

```bash
hubble observe --server localhost:4245 --namespace "$NAMESPACE" --http-status 503 --since 30s
```

**Expected:** a positive backend 5xx rate and HTTP 503 flows for score
operations. The requested error probability is 50%; a small sample need
not be exactly half. Do not count unrelated failures as injected errors.

**Reset:** follow [Reset](#reset); new 503 flows should stop and the rate
should fall as old samples leave the two-minute window.

## 5. Score writes denied

**Run:**

```bash
helm upgrade pacman-fault "$CHART" -n "$NAMESPACE" --reset-values \
  -f "$DEMO_DIR/values/baseline.yaml" --set ciliumNetworkPolicy.denyScoreWrites=true \
  --wait --timeout 5m
```

**Observe:** let the simulator attempt score submissions, then compare writes
with a read:

```bash
hubble observe --server localhost:4245 --namespace "$NAMESPACE" \
  --http-path '/internal/score/write' --since 1m
curl -fsS http://localhost:8080/highscores/list
```

**Expected:** Cilium denies internal score writes, while the read still
returns successfully. Inspect the denied path and verdict/HTTP response;
a failed gameplay request alone does not identify a policy denial.

**Reset:** follow [Reset](#reset); subsequent simulator writes should succeed.

## 6. Missing L7 visibility

**Run:** first confirm the baseline has HTTP flows, then remove its L7 policy:

```bash
helm upgrade pacman-fault "$CHART" -n "$NAMESPACE" --reset-values \
  -f "$DEMO_DIR/values/baseline.yaml" --set ciliumNetworkPolicy.enabled=false \
  --wait --timeout 5m
```

**Observe:** allow policy propagation and at least 30 seconds of new traffic:

```bash
hubble observe --server localhost:4245 --namespace "$NAMESPACE" --since 30s
hubble observe --server localhost:4245 --namespace "$NAMESPACE" --protocol http --since 30s
curl -fsS http://localhost:8080/highscores/list
```

**Expected:** L3/L4 flows and working requests remain, but fresh HTTP flow
details disappear. An empty HTTP result is only meaningful when traffic is
still flowing and the earlier baseline showed HTTP records.

**Reset:** follow [Reset](#reset); fresh HTTP details should return.

## 7. Hubble HTTP exporter disabled

This changes Cilium's cluster-wide metrics configuration. Use only the
disposable cluster, and restore it before moving to another scenario.

**Run:** confirm `hubble_http_requests_total` has samples in Prometheus first.

```bash
helm upgrade cilium cilium/cilium --version 1.19.4 -n kube-system --reuse-values \
  --set 'hubble.metrics.enabled={dns,drop,tcp,flow,port-distribution,icmp}' \
  --wait --timeout 5m
kubectl --context "$CONTEXT" -n kube-system rollout status daemonset/cilium --timeout=5m
```

**Observe:** wait for new scrapes, then compare these instant queries:

```promql
hubble_http_requests_total
```

```promql
hubble_flows_processed_total
```

```bash
hubble observe --server localhost:4245 --namespace "$NAMESPACE" --protocol http --since 30s
```

**Expected:** HTTP metric series disappear after staleness processing, but
Hubble flow metrics, healthy Hubble scrape targets, and new HTTP flow records
remain. An HTTP flow and an exported HTTP metric are different signals.

**Reset:** restore HTTP export, then perform the shared [Reset](#reset):

```bash
helm upgrade cilium cilium/cilium --version 1.19.4 -n kube-system --reuse-values \
  -f "$DEMO_DIR/values/cilium.yaml" --wait --timeout 5m
```

Check that fresh `hubble_http_requests_total` samples return.

## 8. Metrics scrape failure

**Run:**

```bash
helm upgrade pacman-fault "$CHART" -n "$NAMESPACE" --reset-values \
  -f "$DEMO_DIR/values/baseline.yaml" --set serviceMonitor.enabled=false \
  --wait --timeout 5m
```

**Observe:** inspect Prometheus's Targets page after reconciliation:

```bash
kubectl --context "$CONTEXT" -n "$NAMESPACE" get servicemonitors
curl -fsS http://localhost:8080/healthz
hubble observe --server localhost:4245 --namespace "$NAMESPACE" --protocol http --since 30s
```

**Expected:** all three Pac-Man scrape targets disappear, while the app is
healthy and Hubble traffic and scrape targets remain available. This removes
discovery rather than deliberately making an app endpoint fail. Historical
app metrics can remain visible for a while.

**Reset:** follow [Reset](#reset); all three app targets must become UP again.

## Check the Tetragon policy

The security overlays pause the simulator at zero replicas. The chart
requires a matching probe schedule, but no simulator pod runs it: trigger
each probe manually with curl. Keep the simulator paused until the shared
reset, which restores normal simulated gameplay.

Use this check after **each** security-policy install or mode change below.
Find the Tetragon agent on the current frontend's node:

```bash
NODE="$(kubectl --context "$CONTEXT" -n "$NAMESPACE" get pods \
  -l app.kubernetes.io/component=frontend -o jsonpath='{.items[0].spec.nodeName}')"
TETRAGON_POD="$(kubectl --context "$CONTEXT" -n kube-system get pods \
  -l app.kubernetes.io/name=tetragon --field-selector "spec.nodeName=$NODE" \
  -o jsonpath='{.items[0].metadata.name}')"
kubectl --context "$CONTEXT" -n kube-system exec "$TETRAGON_POD" -c tetragon \
  -- tetra tracingpolicy list
```

The named policy in `pacman-fault-demo` must be enabled without a load error.
If it is not, stop and inspect that agent's `tetragon` container logs. A
Kubernetes policy object by itself does not prove the kernel loaded it.

## 9. Synthetic file read: observe, then enforce

**Run (observe):**

```bash
helm upgrade pacman-fault "$CHART" -n "$NAMESPACE" --reset-values \
  -f "$DEMO_DIR/values/baseline.yaml" -f "$DEMO_DIR/values/file-probe.yaml" \
  --wait --timeout 5m
```

Check [policy readiness](#check-the-tetragon-policy) for
`pacman-fault-file-probe`, then trigger the fixed synthetic file read:

```bash
curl -i -X POST -H "x-demo-token: $DEMO_TOKEN" \
  http://localhost:8080/demo/incidents/file-probe
kubectl --context "$CONTEXT" -n kube-system logs "$TETRAGON_POD" -c export-stdout --since=1m
```

**Observe / expected:** the request succeeds. Find a fresh `sys_openat` event
for this frontend pod and `/app/demo-fixtures/synthetic-secret.txt`. The file
contains demo text, not a real secret.

**Run (enforce):**

```bash
helm upgrade pacman-fault "$CHART" -n "$NAMESPACE" --reset-values \
  -f "$DEMO_DIR/values/baseline.yaml" -f "$DEMO_DIR/values/file-probe.yaml" \
  --set tetragon.fileProbeMode=enforce --wait --timeout 5m
```

Repeat the readiness check, POST, and log command. **Expected:** the response
is non-2xx without file content, and a fresh matching Tetragon event reports
`Override` with error `-13` (`EACCES`). Verify `/healthz`, `/highscores/list`,
and normal gameplay still work. An HTTP failure without that event is not
proof of enforcement.

**Reset:** follow [Reset](#reset); the file policy disappears and the probe
is disabled.

## 10. Child execution: observe, then enforce

**Run (observe):**

```bash
helm upgrade pacman-fault "$CHART" -n "$NAMESPACE" --reset-values \
  -f "$DEMO_DIR/values/baseline.yaml" -f "$DEMO_DIR/values/exec-probe.yaml" \
  --wait --timeout 5m
```

Check [policy readiness](#check-the-tetragon-policy) for
`pacman-fault-exec-probe`, then run the fixed, short-lived helper:

```bash
curl -i -X POST -H "x-demo-token: $DEMO_TOKEN" \
  http://localhost:8080/demo/incidents/exec-probe
kubectl --context "$CONTEXT" -n kube-system logs "$TETRAGON_POD" -c export-stdout --since=1m
```

**Observe / expected:** the request succeeds with the helper's marker output,
and Tetragon records `sys_execve` for `/app/tools/demo-exec-probe.js` in this
frontend pod.

**Run (enforce):**

```bash
helm upgrade pacman-fault "$CHART" -n "$NAMESPACE" --reset-values \
  -f "$DEMO_DIR/values/baseline.yaml" -f "$DEMO_DIR/values/exec-probe.yaml" \
  --set tetragon.execProbeMode=enforce --wait --timeout 5m
```

Repeat the readiness check, POST, and log command. **Expected:** the helper
request fails and the matching Tetragon event reports `Sigkill`. The
frontend parent must remain alive: verify `/healthz`, `/highscores/list`,
and gameplay. A timeout or frontend crash is not a successful demonstration.

**Reset:** follow [Reset](#reset); the exec policy disappears and the probe
is disabled.

## Reset

If you changed Cilium's exporter, restore it using scenario 7 first. Then
restore the full app baseline, not the previous scenario's saved Helm values:

```bash
helm upgrade pacman-fault "$CHART" -n "$NAMESPACE" --reset-values \
  -f "$DEMO_DIR/values/baseline.yaml" --wait --timeout 5m
kubectl --context "$CONTEXT" -n "$NAMESPACE" get cnp,servicemonitors,tracingpoliciesnamespaced
```

The score L7 policy and ServiceMonitor should exist; neither probe
TracingPolicyNamespaced should remain. Repeat the [healthy baseline
observations](#1-healthy-baseline), including all three UP scrape targets.
Allow two minutes for old fault samples to leave rate calculations before
starting another scenario. If reset fails, stop and repair this lab or
delete it; do not continue on an unknown baseline.

## Cleanup

Stop the three foreground port-forwards with Ctrl-C. Delete only the
disposable cluster created for this walkthrough:

```bash
kind delete cluster --name "$CLUSTER_NAME"
unset DEMO_TOKEN HELM_KUBECONTEXT
```

The app source checkout, pulled chart, and locally built image under `LAB_DIR`
are left for inspection. No shared cluster or existing checkout needs changing.
