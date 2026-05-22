# Fault injection and resilience demo

The app includes lab-only fault injection middleware. A fault is only injected when `DEMO_SECURITY_MODE=true` and the request has the correct `x-demo-token` header.

## Run

```bash
../scripts/deploy-demo.sh fault-injection-resilience
./trigger-fault.sh
```

## Observe

```bash
hubble observe --namespace pacman-demo --http-status 503
kubectl -n pacman-demo logs deploy/pacman-fault-demo
```

Expected result: the request returns a controlled `503` after the injected delay, and Hubble shows the HTTP error without needing to break the cluster.
