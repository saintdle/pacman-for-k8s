# Tetragon enforcement demo

This demo deploys Pac-Man with lab-only incident probes enabled and a Tetragon `TracingPolicy` that observes process execution and file-open activity.

## Run

```bash
../scripts/deploy-demo.sh tetragon-enforcement
./trigger-probes.sh
```

## Observe

```bash
kubectl -n kube-system logs -l app.kubernetes.io/name=tetragon -c export-stdout --follow
```

Expected result: the file probe opens `/etc/hostname`, and the exec probe starts a short-lived Node child process. Tetragon should emit process/file events tied to the Pac-Man pod.

The policy is observe-only by default. Add enforcement actions only in a disposable lab.
