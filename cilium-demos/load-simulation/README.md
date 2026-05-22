# Load simulation demo

This demo adds traffic generators to an existing Pac-Man deployment. Use it after `microservices-east-west` or any demo that exposes a `pacman-web` Service in the same namespace.

It has two layers:

- `pacman-user-simulator`: runs the app image as continuous synthetic users. It fetches the frontend shell, reads config/location, creates user IDs, posts stats, reads highscores, and submits plausible scores.
- `pacman-k6-load`: runs a finite k6 job for higher-volume HTTP scenario load.

## Run

Deploy a target app first:

```bash
../scripts/deploy-demo.sh microservices-east-west
```

Then deploy the simulators:

```bash
../scripts/deploy-demo.sh load-simulation
```

The simulator deployment runs continuously. The k6 job ramps up, holds load, and exits.

## Scale frontend pods during load

```bash
./scale-frontends.sh 6
kubectl -n pacman-demo get endpointslices -l kubernetes.io/service-name=pacman-web
```

Scale the simulator itself when you want more source pods and more concurrent sessions:

```bash
kubectl -n pacman-demo scale deploy/pacman-user-simulator --replicas=5
```

## Watch traffic

```bash
./watch-load.sh
hubble observe --namespace pacman-demo --from-label app.kubernetes.io/component=load-generator --follow
hubble observe --namespace pacman-demo --to-label app.kubernetes.io/component=web --protocol http
hubble observe --namespace pacman-demo --to-label app.kubernetes.io/component=score --protocol http
```

## Tune load

Patch the simulator deployment:

```bash
kubectl -n pacman-demo set env deploy/pacman-user-simulator USERS=100 MIN_THINK_MS=500 MAX_THINK_MS=2000
```

Run another k6 job by deleting the completed one and applying the demo again:

```bash
kubectl -n pacman-demo delete job pacman-k6-load --ignore-not-found
kubectl -n pacman-demo apply -k .
```

Useful environment values:

```bash
PACMAN_BASE_URL=http://pacman-web:8080
USERS=100
DURATION_SECONDS=0
SCORE_SUBMIT_RATE=0.08
HIGH_SCORE_READ_RATE=0.35
```

## Demo ideas

- Scale `pacman-web` while load is active and observe Hubble distributing flows across new pods.
- Roll out the canary demo while k6 is running and compare `/version` responses.
- Apply stricter CiliumNetworkPolicy and confirm load-generator-to-web is allowed while direct load-generator-to-score is denied.
- Trigger the fault-injection demo during active sessions and watch retries, failed requests, and latency changes.
