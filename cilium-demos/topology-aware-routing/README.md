# Topology-aware routing demo

This demo spreads Pac-Man replicas across two kind worker zones and annotates the Service with `service.kubernetes.io/topology-mode: Auto`.

The app reads Kubernetes metadata and displays topology-aware routing details through `/location/metadata` and the game UI.

## Run

```bash
../scripts/deploy-demo.sh topology-aware-routing
kubectl -n pacman-demo port-forward svc/pacman-topology 8080:8080
```

## Observe

```bash
kubectl -n pacman-demo get endpointslice -l kubernetes.io/service-name=pacman-topology -o yaml
curl localhost:8080/location/metadata
```

Look for `topology-aware` in the returned `zone` field when hints or service topology mode are visible to the app.
