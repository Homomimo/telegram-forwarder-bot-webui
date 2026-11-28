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
  -v /root/telegram-forwarder/downloads:/downloads \
  -e API_ID='你的API_ID' \
  -e API_HASH='你的API_HASH' \
  -e BOT_TOKEN='你的Bot Token' \
  -e ADMIN_IDS='你的Telegram用户ID' \
  ghcr.io/homomimo/telegram-forwarder-bot-webui:latest
```

容器启动时会通过 HTTPS 查询公网 IPv4，用于 WebUI 地址、证书和 Bot 登录链接。需要手动指定地址时，在 `docker run` 里加 `-e RELAY_VPS_IP='你的VPS公网IPv4'`，或在运行 `start.sh` 前导出同名变量。手动设置优先。查询失败时，容器会在启动日志中提示检查 IPv4 出网或手动指定。经过共享 NAT 等环境时，请手动指定浏览器实际能访问的 IPv4。

管理员在 Bot 私聊中发送 `/start`，点击首页第一行的「🌐 打开 WebUI」，再打开 Bot 发来的一次性登录链接。链接 2 分钟内可用一次。链接失效时，回 Bot 点击同一个按钮获取新链接。没有有效会话时，直接访问 WebUI 会显示「请从 Telegram Bot 打开」。

用下面的命令查看实际 WebUI 地址和 `HTTPS SHA256`，首次访问时与浏览器显示的证书指纹核对。自动模式下，公网 IP 变更后重启会使用新地址的证书，请重新核对指纹并从 Bot 生成新链接。

```bash
docker logs telegram-forwarder-bot 2>&1 | grep -E '\[启动/WebUI\]|HTTPS SHA256:'
```

若把宿主机端口改为例如 `9443`，请在 `docker run` 中同时使用 `-p 9443:8765` 和 `-e WEB_PORT=9443`，并在安全组放行对应端口。

> 命令中的凭据会留在终端历史中，也可被有 Docker 管理权限的人通过 `docker inspect` 查看。请勿公开日志或泄露凭据。

## 首次配置

登录 WebUI 后，打开「工作台设置」。在所需账号的卡片上点「扫码登录」，再点「生成二维码」并用 Telegram 扫码。

- **TDL 转发账号**用于读取源帖和必要时发布内容。手动无引用转发、批量转发，或自动复制遇到禁转并通过 TDL 重新发布时，需要登录此账号。
- **Telethon 用户会话**用于通过个人 Telegram 账号监听源频道。如果 Bot 已能收到所需频道的更新，可以跳过。首次登录默认使用 `docker run` 中的 `API_ID` 和 `API_HASH`，重新扫码默认沿用原会话的应用凭据。

在网页设置转发目标和监听来源。也可在 Bot 中用 `/forwardto <频道ID>` 设置目标，用 `/monitor add <频道ID>` 添加来源。先在自己的测试频道验证，再用于正式频道。只转发你有权复制的内容。Bot 私聊中的 `/help` 也可以查看扫码向导。

### 频道配对

在 WebUI 的「频道配对」或 Bot 的「📡 监听频道 → 🔀 频道配对」中，为每个源配置一个独立目标。升级后仍按原配置自动复制，配对在确认「启用严格一对一」前只作为草稿保存。启用前需为所有现有监听源配置并启用配对。切换后，可在同一页面新增、改绑、暂停或删除配对。

每个源只绑定一个目标，每个目标只接受一个配对源。自动目标和手动默认目标都不能同时作为配对源。手动投稿和批量转发继续使用默认目标。「手动同步」和 `/resync` 按源频道的当前配对查找对应目标帖。改绑不补发历史，已入队任务保留入队时的目标。

要恢复默认目标频道，在 Bot 或 WebUI 的「频道配对」页点击「关闭严格一对一…」，核对确认页显示的旧版监听源和默认目标频道后确认。关闭后配对和源帖映射仍保留，已入队任务仍发往入队时的目标。未设置旧版监听源或默认目标频道时，关闭后不会自动转发。

在 WebUI 或 Bot 管理面板打开「源帖映射」，查看已确认自动复制的源帖及其对应的目标帖。管理员也可在 Bot 私聊中发送 `/mappings [页码]`。

列表有记录时，可选择「清理全部映射…」并确认。原帖和目标帖会保留，已复制的帖子仍会去重。清理后的旧帖失去编辑同步和手动同步所需的对应关系，新帖会继续建立映射。

## 下载暂存

- 在 Bot 管理员首页点「📥 下载暂存」，回复原帖链接，选择整组相册或单项。下载使用已登录的 TDL 账号读取媒体，Bot 或已连接的 Telethon 用户会话也需要能读取源帖以核对媒体和相册。
- WebUI 的「文件暂存区」支持预览图片和浏览器可播放的视频。在 Bot 的「暂存文件列表」可以勾选删除或清理全部已下载文件。正在下载的文件不会被清理。

## 在 VPS 上更新

镜像发布后不会自动替换运行中的容器。先在 Bot 中用 `/queue` 确认没有正在转发或等待的任务，再以 **root** 在 VPS 上执行（需要 Python 3、curl 和 GNU tar，无需克隆仓库或重填 Token）：

```bash
curl -fsSL https://raw.githubusercontent.com/Homomimo/telegram-forwarder-bot-webui/main/update.sh -o /root/telegram-forwarder/update.sh && sh /root/telegram-forwarder/update.sh
```

旧容器只有 `/data` 时，可以照常更新。要同时启用文件暂存区，使用已下载的更新脚本显式添加一个独立目录：

```bash
sh /root/telegram-forwarder/update.sh --downloads /root/telegram-forwarder/downloads
```

后续更新会沿用原有挂载。更新前请在 Bot 的「📥 下载暂存 → 查看文件任务」确认文件任务已完成。`/downloads` 中的媒体可按需单独备份。`/data` 的停机备份包含文件目录索引。

脚本拉取 `latest`。镜像与挂载都没变时无需停机。有更新时先停机备份 `/data` 到 `/root/telegram-forwarder/backups/`，再沿用原容器配置启动。已有的 `RELAY_VPS_IP` 会继续作为手动配置。健康检查使用容器实际启用的 WebUI 地址。看到「更新成功」后，在 Bot 私聊发送 `/start`，点击「🌐 打开 WebUI」并打开新发来的一次性链接，验证外部访问。

若更新失败，脚本会尝试恢复旧容器。若 VPS 断电或回退失败，先用 `docker ps -a` 检查 `.previous`、`.next` 和正式容器，确认数据目录完整后再手动启动。备份含账号会话和 HTTPS 私钥，请妥善保管，按需清理。

## 注意事项

- `ADMIN_IDS` 填 Telegram **用户 ID**，不是频道 ID；多个管理员用逗号分隔。
- `API_ID` 和 `API_HASH` 从 Telegram 应用设置获取；不能用 Bot Token 代替。
- 按一键启动命令部署时，容器内的 `/data` 对应宿主机的 `/root/telegram-forwarder/data`。同一数据目录只供一个容器使用。手动备份前先停止容器。
- `/queue` 显示「等待相册补齐」时，如果目标频道没有这组消息，先在源频道数清这一个相册的项数，再私聊 Bot 发送 `/queue album <任务ID> <实际项数>`（2–10）。数量不符不会发送。不要另外手动转发同一相册。
- 发送结果不确定时，任务会保留待核对状态，不会自动重发。核对目标频道后再处理，避免重复发送。

更多 Bot 命令、转发回执和相册排查示例见[机器人使用指南](BOT-UPSTREAM.md#命令速查)。
