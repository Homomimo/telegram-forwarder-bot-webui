#!/usr/bin/env bash
# Run against the built image. No Telegram secrets, host data, or inbound ports.
set -euo pipefail
version=$(cat VERSION)
image="telegram-relay-ci:$version"
docker build --build-arg "APP_VERSION=$version" -t "$image" .
docker run --rm --network none --entrypoint python \
  -v "$PWD/tests:/app/tests:ro" "$image" -B -m unittest discover -s /app/tests -q
docker run --rm --network none --entrypoint sh "$image" \
  -ec 'tdl forward --help | grep -q -- "--relay-edit-target"'
container=$(docker run -d --network none -e RELAY_MODE=demo -e RELAY_LOG_PASSWORD=0 "$image")
trap 'docker rm -fv "$container" >/dev/null' EXIT
for ((attempt=0; attempt<60; attempt++)); do
  if docker exec "$container" python -c 'from urllib.request import urlopen; urlopen("http://127.0.0.1:8765/api/auth/session", timeout=2).close()' 2>/dev/null; then
    docker exec "$container" python /app/tools/verify_demo.py
    exit 0
  fi
  sleep 1
done
echo "Offline Demo did not become ready" >&2
exit 1
