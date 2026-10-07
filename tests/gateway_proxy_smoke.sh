#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_DIR="$(mktemp -d)"
SUFFIX="$$"
NETWORK="radar-gateway-test-$SUFFIX"
UPSTREAM="radar-gateway-upstream-$SUFFIX"
PUBLIC_GATEWAY="radar-gateway-public-$SUFFIX"
LOCAL_GATEWAY="radar-gateway-local-$SUFFIX"
EDGE_SECRET=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef

cleanup() {
  docker rm -f "$PUBLIC_GATEWAY" "$LOCAL_GATEWAY" "$UPSTREAM" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
  rm -f "$TEMP_DIR/upstream.conf"
  rmdir "$TEMP_DIR" 2>/dev/null || true
}
trap cleanup EXIT

cat > "$TEMP_DIR/upstream.conf" <<'NGINX'
server {
    listen 8005;
    location / {
        default_type text/plain;
        return 200 "$http_x_real_ip|$http_x_forwarded_proto|$http_x_forwarded_for|$http_x_radar_edge_secret";
    }
}
NGINX

docker network create "$NETWORK" >/dev/null
docker run -d --name "$UPSTREAM" --network "$NETWORK" \
  --network-alias fastapi_api --network-alias frontend \
  --mount "type=bind,source=$TEMP_DIR/upstream.conf,target=/etc/nginx/conf.d/default.conf,readonly" \
  nginx:1.30.5-alpine >/dev/null

gateway_mounts=(
  --mount "type=bind,source=$ROOT/nginx.conf,target=/etc/nginx/templates/default.conf.template,readonly"
  --mount "type=bind,source=$ROOT/gateway.envsh,target=/docker-entrypoint.d/18-radar-host-gateway.envsh,readonly"
)
docker run -d --name "$PUBLIC_GATEWAY" --network "$NETWORK" -p 127.0.0.1::8080 \
  -e RADAR_ALLOWED_ORIGINS=https://radar.example.com \
  -e "RADAR_EDGE_SHARED_SECRET=$EDGE_SECRET" \
  "${gateway_mounts[@]}" nginx:1.30.5-alpine >/dev/null
docker run -d --name "$LOCAL_GATEWAY" --network "$NETWORK" -p 127.0.0.1::8080 \
  -e RADAR_ALLOWED_ORIGINS=http://localhost:8080 \
  "${gateway_mounts[@]}" nginx:1.30.5-alpine >/dev/null

public_port="$(docker port "$PUBLIC_GATEWAY" 8080/tcp)"
local_port="$(docker port "$LOCAL_GATEWAY" 8080/tcp)"
public_url="http://$public_port/api/v1/auth/login"
local_url="http://$local_port/api/v1/auth/login"

request() {
  curl --retry 8 --retry-connrefused --retry-delay 1 --fail --silent --show-error "$@"
}
assert_equal() {
  if [[ "$1" != "$2" ]]; then
    printf 'Gateway mismatch: expected %s, got %s\n' "$2" "$1" >&2
    exit 1
  fi
}

a="$(request -H 'X-Forwarded-Proto: https' -H 'X-Forwarded-For: 198.51.100.10' -H "X-Radar-Edge-Secret: $EDGE_SECRET" "$public_url")"
b="$(request -H 'X-Forwarded-Proto: https' -H 'X-Forwarded-For: 198.51.100.11' -H "X-Radar-Edge-Secret: $EDGE_SECRET" "$public_url")"
upper="$(request -H 'X-Forwarded-Proto: HTTPS' -H 'X-Forwarded-For: 198.51.100.12' -H "X-Radar-Edge-Secret: $EDGE_SECRET" "$public_url")"
missing_ip="$(request -H 'X-Forwarded-Proto: https' -H "X-Radar-Edge-Secret: $EDGE_SECRET" "$public_url")"
duplicate_ip="$(request -H 'X-Forwarded-Proto: https' -H 'X-Forwarded-For: 198.51.100.10' -H 'X-Forwarded-For: 198.51.100.11' -H "X-Radar-Edge-Secret: $EDGE_SECRET" "$public_url")"
missing_secret="$(request -H 'X-Forwarded-Proto: https' -H 'X-Forwarded-For: 198.51.100.13' "$public_url")"
wrong_secret="$(request -H 'X-Forwarded-Proto: https' -H 'X-Forwarded-For: 198.51.100.14' -H 'X-Radar-Edge-Secret: wrong' "$public_url")"
local_spoof="$(request -H 'X-Forwarded-Proto: https' -H 'X-Forwarded-For: 203.0.113.77' -H "X-Radar-Edge-Secret: $EDGE_SECRET" "$local_url")"
untrusted="$(docker exec "$UPSTREAM" wget -q -O - --header='X-Forwarded-Proto: https' --header='X-Forwarded-For: 203.0.113.88' --header="X-Radar-Edge-Secret: $EDGE_SECRET" "http://$PUBLIC_GATEWAY:8080/api/v1/auth/login")"

assert_equal "$a" '198.51.100.10|https|198.51.100.10|'
assert_equal "$b" '198.51.100.11|https|198.51.100.11|'
assert_equal "$upper" '198.51.100.12|https|198.51.100.12|'
assert_equal "$missing_ip" '|https||'
assert_equal "$duplicate_ip" '|https||'
assert_equal "$missing_secret" '|http||'
assert_equal "$wrong_secret" '|http||'
[[ "$local_spoof" != *203.0.113.77* && "$local_spoof" != *'|https|'* && "$local_spoof" == *'|http|'* ]] || {
  printf 'Local forwarding spoof changed identity: %s\n' "$local_spoof" >&2
  exit 1
}
assert_equal "$untrusted" '|http||'
printf 'Gateway proxy identity smoke passed.\n'
