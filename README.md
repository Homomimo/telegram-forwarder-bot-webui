# Telegram Forwarder Bot + WebUI

通过 Telegram Bot 和网页管理频道转发。需要一台装有 Docker 的 Linux VPS，以及 Telegram Bot Token、API ID、API Hash 和管理员的 Telegram 用户 ID。

## 一键启动

将命令中的示例值替换为自己的信息。开放 VPS 的 TCP **8765** 端口，建议只允许自己的 IP 访问。

```bash
docker run -d --init --restart unless-stopped \
  --name telegram-forwarder-bot \
  --security-opt no-new-privileges \
  --log-opt max-size=10m --log-opt max-file=3 \
  -p 8765:8765 \
  -v /root/telegram-forwarder/data:/data \
  -e RELAY_VPS_IP='你的VPS公网IPv4' \
  -e API_ID='你的API_ID' \
  -e API_HASH='你的API_HASH' \
  -e BOT_TOKEN='你的Bot Token' \
  -e ADMIN_IDS='你的Telegram用户ID' \
  ghcr.io/homomimo/telegram-forwarder-bot-webui:latest
```

Docker 会自动拉取镜像并启动。查看登录密码：

```bash
docker logs telegram-forwarder-bot
```

打开 `https://你的VPS公网IPv4:8765`，首次访问时核对浏览器证书指纹与日志中的 `HTTPS SHA256`。使用用户名 `admin` 和日志中的 `Web password` 登录。

> 命令中的凭据会留在终端历史中，也可被有 Docker 管理权限的人通过 `docker inspect` 查看。请勿公开日志或泄露凭据。

## 首次配置

1. 在网页「工作台设置」登录 **TDL 转发账号**，用于读取来源消息和发布转发内容。
2. 如需自动监听，再登录 **Telethon 用户账号**；这是独立于 Bot Token 的用户会话。
3. 在网页设置转发目标和监听来源。也可在 Bot 中用 `/forwardto <频道ID>` 设置目标，用 `/monitor add <频道ID>` 添加来源。
4. 先在自己的测试频道验证，再用于正式频道。只转发你有权复制的内容。

## 注意事项

- `ADMIN_IDS` 填 Telegram **用户 ID**，不是频道 ID；多个管理员用逗号分隔。
- `API_ID` 和 `API_HASH` 从 Telegram 应用设置获取；不能用 Bot Token 代替。
- 数据保存在 `/root/telegram-forwarder/data`。同一数据目录不要同时运行多个容器；备份前先停止容器。
- 发送结果不确定时，任务会保留待核对状态，不会自动重发。核对目标频道后再处理，避免重复发送。
- `latest` 指向最新发布版本；运行中的容器不会自动更新，更新时需重新拉取镜像并重建容器。
