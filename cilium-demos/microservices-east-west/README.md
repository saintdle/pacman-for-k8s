# Microservices east-west demo

This demo runs one Pac-Man image as three roles: `web`, `score`, and `user`. The browser still talks to the web service, but score and user calls become real east-west Service traffic that Cilium and Hubble can observe and enforce.

## Run

```bash
../scripts/deploy-demo.sh microservices-east-west
kubectl -n pacman-demo port-forward svc/pacman-web 8080:8080
```

Open <http://localhost:8080>, play, and save a score.

## Observe

```bash
hubble observe --namespace pacman-demo --from-label app.kubernetes.io/component=web --follow
hubble observe --namespace pacman-demo --to-label app.kubernetes.io/component=score --protocol http
```

Expected flows include `GET /user/id`, `POST /user/stats`, `GET /highscores/list`, and `POST /highscores` between app roles.

## Policy point

`cilium-network-policy.yaml` allows only web-to-score and web-to-user HTTP paths needed by the app. Try calling an unlisted path on `pacman-score` from another pod and observe a policy drop.
