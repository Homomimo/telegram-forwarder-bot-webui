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

管理员也可以在机器人私聊中发送 `/start`，选择「⚙️ 系统管理 → 🌐 打开 WebUI」。按钮只打开网页，不会代为登录。若把宿主机端口改为例如 `9443`，请在 `docker run` 中同时使用 `-p 9443:8765` 和 `-e WEB_PORT=9443`，并在安全组放行对应端口。

> 命令中的凭据会留在终端历史中，也可被有 Docker 管理权限的人通过 `docker inspect` 查看。请勿公开日志或泄露凭据。

## 首次配置

1. 在网页「工作台设置」登录 **TDL 转发账号**，用于读取来源消息和发布转发内容。
2. 如需自动监听，在 **Telethon 用户账号** 下点「生成二维码」并扫码。首次登录默认使用 `docker run` 中的 `API_ID` 和 `API_HASH`，不用再填；重新扫码默认沿用原会话的应用凭据。
3. 在网页设置转发目标和监听来源。也可在 Bot 中用 `/forwardto <频道ID>` 设置目标，用 `/monitor add <频道ID>` 添加来源。
4. 先在自己的测试频道验证，再用于正式频道。只转发你有权复制的内容。

在 WebUI 的「同步映射」或 Bot 管理面板的「同步映射」查看已确认的自动同步关系。管理员也可在 Bot 私聊中发送 `/mappings [页码]`。

## 在 VPS 上更新

镜像发布后不会自动替换运行中的容器。先在 Bot 中用 `/queue` 确认没有正在转发或等待的任务，再以 **root** 在 VPS 上执行（需要 Python 3、curl 和 GNU tar，无需克隆仓库或重填 Token）：

```bash
curl -fsSL https://raw.githubusercontent.com/Homomimo/telegram-forwarder-bot-webui/main/update.sh -o /root/telegram-forwarder/update.sh && sh /root/telegram-forwarder/update.sh
```

脚本拉取 `latest`；镜像没变就不停机。有更新时先停机备份 `/data` 到 `/root/telegram-forwarder/backups/`，再用原凭据、端口和挂载启动新容器。看到「更新成功」表示已收到 Bot 就绪日志，且容器内的 WebUI 检查通过。可再发送 `/start` 并打开 WebUI，验证外部访问。

新镜像直接把文件存到 `/data`。如果你已有旧版的 `/data/live`，请在新镜像发布后用上面的命令更新。脚本会在停机备份后迁移频道配置、会话、历史和队列等数据，宿主机挂载路径不变；不要直接拿旧目录启动新镜像。

若更新失败，脚本会尝试恢复旧容器；需要回退布局时，会把**当前文件**移回 `live`，而不是用备份覆盖数据库，以免重复投递。若 VPS 断电或回退失败，先用 `docker ps -a` 检查 `.previous`、`.next` 和正式容器，确认数据布局完整之前不要手动启动任一容器。备份含账号会话与密码，请妥善保管，按需清理。

## 注意事项

- `ADMIN_IDS` 填 Telegram **用户 ID**，不是频道 ID；多个管理员用逗号分隔。
- `API_ID` 和 `API_HASH` 从 Telegram 应用设置获取；不能用 Bot Token 代替。
- 数据直接保存在宿主机的 `/root/telegram-forwarder/data`，不再加 `live` 子目录。同一数据目录不要同时运行多个容器；备份前先停止容器。
- `/queue` 显示「等待相册补齐」时，如果目标频道没有这组消息，先在源频道数清这一个相册的项数，再私聊 Bot 发送 `/queue album <任务ID> <实际项数>`（2–10）。数量不符不会发送。不要另外手动转发同一相册。
- 发送结果不确定时，任务会保留待核对状态，不会自动重发。核对目标频道后再处理，避免重复发送。

更多 Bot 命令、转发回执和相册排查示例见[机器人使用指南](BOT-UPSTREAM.md#命令速查)。
