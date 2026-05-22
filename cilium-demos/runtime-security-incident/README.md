# Runtime security incident demo

This demo creates a safe incident story: a suspicious score attempt is recorded by the app, rejected by server-side validation, visible in logs, and observable alongside Cilium/Hubble traffic.

## Run

```bash
../scripts/deploy-demo.sh runtime-security-incident
./trigger-incident.sh
```

## Observe

```bash
kubectl -n pacman-demo logs deploy/pacman-incident-demo
hubble observe --namespace pacman-demo --http-status 400
```

Expected result: `/demo/incidents` logs a structured incident, and `/highscores` rejects the implausible score with `400`.
