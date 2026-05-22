#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-pacman-demo}"
TOKEN="${DEMO_TOKEN:-pacman-demo}"

kubectl -n "$NAMESPACE" port-forward svc/pacman-incident-demo 8080:8080 >/tmp/pacman-incident-port-forward.log 2>&1 &
PF_PID=$!
trap 'kill "$PF_PID" >/dev/null 2>&1 || true' EXIT

for _ in {1..20}; do
  if curl -fsS http://127.0.0.1:8080/version >/dev/null 2>&1; then break; fi
  sleep 1
done

curl -fsS -X POST \
  -H "content-type: application/json" \
  -H "x-demo-token: $TOKEN" \
  -d '{"type":"score-tamper","action":"suspicious-score-submit"}' \
  http://127.0.0.1:8080/demo/incidents | jq .

curl -i -X POST http://127.0.0.1:8080/highscores -d 'name=CHEATER&score=999999&level=1'
