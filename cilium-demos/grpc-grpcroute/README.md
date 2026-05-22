# gRPC and GRPCRoute demo

This demo is the Kubernetes side of the gRPC use case. It expects an app image with `APP_ROLE=grpc` listening on port `9090` for `pacman.ScoreService`.

## Run

```bash
../scripts/deploy-demo.sh grpc-grpcroute
kubectl -n pacman-demo get gateway,grpcroute,svc pacman-grpc
```

## Observe

Use `grpcurl` against the Gateway address once the app image includes the gRPC server role:

```bash
grpcurl -plaintext <gateway-address>:9090 list
hubble observe --namespace pacman-demo --protocol http --follow
```

The app-side gRPC server is implemented in the application repo; this directory provides the Gateway API wiring for Cilium.
