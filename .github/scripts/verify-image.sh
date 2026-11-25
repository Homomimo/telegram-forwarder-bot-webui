#!/usr/bin/env bash
# Run against the built image. No Telegram secrets, host data, or inbound ports.
set -euo pipefail
version=${APP_VERSION:-$(cat VERSION)}
image="telegram-relay-ci:$version"
docker build --build-arg "APP_VERSION=$version" -t "$image" .
docker run --rm --network none --entrypoint python \
  -v "$PWD/tests:/app/tests:ro" "$image" -B -m unittest discover -s /app/tests -q
docker run --rm --network none --entrypoint sh "$image" \
  -ec 'tdl forward --help | grep -q -- "--relay-edit-target"'
# Production must fail closed without Telegram credentials, even on an isolated network.
if output=$(timeout 30s docker run --rm --network none "$image" 2>&1); then
  echo "Image started without required credentials" >&2
  exit 1
fi
grep -Fq '需要 BOT_TOKEN、ADMIN_IDS、API_ID、API_HASH' <<< "$output" || {
  printf '%s\n' "$output" >&2
  exit 1
}
