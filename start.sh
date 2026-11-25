#!/bin/sh
# Build the image locally (amd64 or arm64) and start exactly one instance.
set -eu
cd "$(dirname "$0")"
mode=${1:-demo}
case "$mode" in
  demo) name=tdl-forwarder-demo; port=${WEB_PORT:-8765} ;;
  live) name=tdl-forwarder; port=${WEB_PORT:-8766}
        : "${RELAY_VPS_IP:?请设置 VPS 公网 IPv4}" "${BOT_TOKEN:?请设置 Bot Token}" "${ADMIN_IDS:?请设置 Telegram 管理员用户 ID}" "${API_ID:?请设置 API ID}" "${API_HASH:?请设置 API Hash}" ;;
  *) echo '用法: sh start.sh demo|live' >&2; exit 2 ;;
esac
version=$(cat VERSION)
repository=telegram-forwarder-bot-webui
image="$repository:$version"
if docker container inspect "$name" >/dev/null 2>&1; then
  echo "容器 $name 已存在。请用 docker start $name，或先备份再手动替换。" >&2; exit 1
fi
docker build --build-arg "APP_VERSION=$version" --tag "$image" --tag "$repository:latest" .
set -- --name "$name" --init --restart unless-stopped --security-opt no-new-privileges --log-opt max-size=10m --log-opt max-file=3 \
       -v /root/telegram-forwarder/data:/data -e "RELAY_MODE=$mode" -e RELAY_LOG_PASSWORD -e RELAY_PASSWORD
if [ -n "${RELAY_VPS_IP:-}" ]; then
  set -- "$@" -p "$port:8765" -e RELAY_VPS_IP
else
  set -- "$@" -p "127.0.0.1:$port:8765"
fi
if [ "$mode" = live ]; then set -- "$@" -e BOT_TOKEN -e ADMIN_IDS -e SUBMITTER_IDS -e API_ID -e API_HASH; fi
docker run -d "$@" "$image"
printf '查看登录信息：docker logs %s\n' "$name"
