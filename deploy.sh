#!/usr/bin/env bash
set -Eeuo pipefail
trap 'printf "Deployment failed at line %s; success was not verified.\n" "$LINENO" >&2' ERR
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
ACTION="${1:-check}"
case "$ACTION" in check|up|status|logs|stop) ;; *) printf 'Usage: bash deploy.sh {check|up|status|logs|stop}\n' >&2; exit 2;; esac
ENV_FILE="${RADAR_ENV_FILE:-$PROJECT_DIR/.env}"
if ! command -v docker >/dev/null 2>&1; then
  printf 'Docker was not found. Install Docker Engine/Desktop and Compose v2.20+.\n' >&2
  exit 127
fi
if [[ ! -f "$ENV_FILE" ]]; then
  printf 'Environment file not found: %s. Complete the setup steps in DEPLOY.md.\n' "$ENV_FILE" >&2
  exit 2
fi
if grep -Eq '^[[:space:]]*(POSTGRES_PASSWORD|NEO4J_PASSWORD|CLICKHOUSE_PASSWORD|RADAR_SESSION_SECRET|RADAR_INGEST_API_KEY|RADAR_USERS_JSON)[[:space:]]*=.*REPLACE_' "$ENV_FILE"; then
  printf 'Required environment settings still contain example values. Complete .env before starting services.\n' >&2
  exit 2
fi
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$PROJECT_DIR/docker-compose.yml")
docker compose version >/dev/null
"${COMPOSE[@]}" config --quiet
case "$ACTION" in
  check)
    printf 'Compose syntax and required interpolation are valid; services have not been started.\n'
    printf 'Application security settings are checked at gateway and API startup, not by this command.\n'
    ;;
  up)
    docker info >/dev/null
    if [[ ! -s requirements.txt ]]; then
      printf 'requirements.txt was not found; the backend will not build without its hash-pinned dependency lockfile.\n' >&2
      exit 2
    fi
    "${COMPOSE[@]}" build
    "${COMPOSE[@]}" up -d --wait --wait-timeout 300
    "${COMPOSE[@]}" exec -T gateway wget -q -O /dev/null http://127.0.0.1:8080/
    "${COMPOSE[@]}" exec -T gateway wget -q -O /dev/null http://127.0.0.1:8080/healthz
    printf 'Selected Compose services are ready; frontend and API access was verified through the gateway: http://127.0.0.1:8080\n'
    printf 'This check does not verify live collection, AI output, or end-to-end data accuracy.\n'
    ;;
  status) "${COMPOSE[@]}" ps ;;
  logs) "${COMPOSE[@]}" logs --tail=100 ;;
  stop) "${COMPOSE[@]}" stop ;;
esac
