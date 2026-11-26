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

管理员在 Bot 私聊中发送 `/start`，点击首页第一行的「🌐 打开 WebUI」，也可以在浏览器直接访问 `https://你的VPS公网IPv4:8765`。首次访问时，核对浏览器证书指纹与日志中的 `HTTPS SHA256`，再使用用户名 `admin` 和日志中的 `Web password` 登录。

若把宿主机端口改为例如 `9443`，请在 `docker run` 中同时使用 `-p 9443:8765` 和 `-e WEB_PORT=9443`，并在安全组放行对应端口。

> 命令中的凭据会留在终端历史中，也可被有 Docker 管理权限的人通过 `docker inspect` 查看。请勿公开日志或泄露凭据。

## 首次配置

登录 WebUI 后，打开「工作台设置」。在所需账号的卡片上点「扫码登录」，再点「生成二维码」并用 Telegram 扫码。

- **TDL 转发账号**用于读取源帖和必要时发布内容。手动无引用转发、批量转发，或自动复制遇到禁转并通过 TDL 重新发布时，需要登录此账号。
- **Telethon 用户会话**用于通过个人 Telegram 账号监听源频道。如果 Bot 已能收到所需频道的更新，可以跳过。首次登录默认使用 `docker run` 中的 `API_ID` 和 `API_HASH`，重新扫码默认沿用原会话的应用凭据。

在网页设置转发目标和监听来源。也可在 Bot 中用 `/forwardto <频道ID>` 设置目标，用 `/monitor add <频道ID>` 添加来源。先在自己的测试频道验证，再用于正式频道。只转发你有权复制的内容。Bot 私聊中的 `/help` 也可以查看扫码向导。

在 WebUI 或 Bot 管理面板打开「源帖映射」，查看已确认自动复制的源帖及其对应的目标帖。管理员也可在 Bot 私聊中发送 `/mappings [页码]`。

列表有记录时，可选择「清理全部映射…」并确认。原帖和目标帖会保留，已复制的帖子仍会去重。清理后的旧帖失去编辑同步和手动同步所需的对应关系，新帖会继续建立映射。

## 在 VPS 上更新

镜像发布后不会自动替换运行中的容器。先在 Bot 中用 `/queue` 确认没有正在转发或等待的任务，再以 **root** 在 VPS 上执行（需要 Python 3、curl 和 GNU tar，无需克隆仓库或重填 Token）：

```bash
curl -fsSL https://raw.githubusercontent.com/Homomimo/telegram-forwarder-bot-webui/main/update.sh -o /root/telegram-forwarder/update.sh && sh /root/telegram-forwarder/update.sh
```

脚本拉取 `latest`；镜像没变就不停机。有更新时先停机备份 `/data` 到 `/root/telegram-forwarder/backups/`，再用原凭据、端口和挂载启动新容器。看到「更新成功」表示已收到 Bot 就绪日志，且容器内的 WebUI 检查通过。可再发送 `/start` 并打开 WebUI，验证外部访问。

若更新失败，脚本会尝试恢复旧容器。若 VPS 断电或回退失败，先用 `docker ps -a` 检查 `.previous`、`.next` 和正式容器，确认数据目录完整后再手动启动。备份含账号会话与密码，请妥善保管，按需清理。

## 注意事项

- `ADMIN_IDS` 填 Telegram **用户 ID**，不是频道 ID；多个管理员用逗号分隔。
- `API_ID` 和 `API_HASH` 从 Telegram 应用设置获取；不能用 Bot Token 代替。
- 按一键启动命令部署时，容器内的 `/data` 对应宿主机的 `/root/telegram-forwarder/data`。同一数据目录只供一个容器使用。手动备份前先停止容器。
- `/queue` 显示「等待相册补齐」时，如果目标频道没有这组消息，先在源频道数清这一个相册的项数，再私聊 Bot 发送 `/queue album <任务ID> <实际项数>`（2–10）。数量不符不会发送。不要另外手动转发同一相册。
- 发送结果不确定时，任务会保留待核对状态，不会自动重发。核对目标频道后再处理，避免重复发送。

更多 Bot 命令、转发回执和相册排查示例见[机器人使用指南](BOT-UPSTREAM.md#命令速查)。
