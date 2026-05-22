# Canary progressive delivery demo

This demo uses Cilium Gateway API to split HTTP traffic between a stable classic Pac-Man deployment and an eBee canary deployment. The app exposes version and variant metadata through `/config` and `/version`.

## Run

```bash
../scripts/deploy-demo.sh canary-progressive-delivery
kubectl -n pacman-demo get gateway,httproute
```

## Observe

```bash
for i in {1..20}; do curl -s http://<gateway-address>/version; echo; done
hubble observe --namespace pacman-demo --protocol http --follow
```

Expected result: roughly 80 percent stable responses and 20 percent canary responses.
