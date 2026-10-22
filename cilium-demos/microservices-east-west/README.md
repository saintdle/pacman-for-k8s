# Microservices east-west demo

This demo runs one Pac-Man image as three roles: `web`, `score`, and `user`. The browser still talks to the web service, but score and user calls become real east-west Service traffic that Cilium and Hubble can observe and enforce.

## Run

```bash
./cilium-demos/scripts/deploy-demo.sh microservices-east-west
kubectl -n pacman-east-west port-forward svc/pacman-web 8080:8080
```

Open <http://localhost:8080>, play, and save a score.
Run the command from the repository root. This demo uses memory-backed
score/user roles in `pacman-east-west`, separate from the persistent base.
Direct `kubectl apply -k cilium-demos/microservices-east-west` uses that same
namespace (create it first). `NAMESPACE` overrides in the wrapper are checked
before mutation; a namespace containing persistent base resources is refused.

## Observe

```bash
hubble observe --namespace pacman-east-west --from-label app.kubernetes.io/component=web --follow
hubble observe --namespace pacman-east-west --to-label app.kubernetes.io/component=score --protocol http
```

Expected flows include `GET /user/id`, `POST /user/stats`, `GET /highscores/list`, and `POST /highscores` between app roles.

## Policy point

`cilium-network-policy.yaml` allows only web-to-score and web-to-user HTTP paths needed by the app. Try calling an unlisted path on `pacman-score` from another pod and observe a policy drop.
