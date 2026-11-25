# Telegram Forwarder · Bot + WebUI

在浏览器和 Telegram Bot 里管理同一套频道规则、队列和投递记录。首个发布版本 **1.0.0**，镜像默认运行离线 Demo。TDL 复用原 Web 镜像内置的 `0.20.4-relay1`，无需在 VPS 单独安装。真实扫码和发送只在 `live` 模式启用。

## 1. 启动 Demo（Linux / Docker）

**还没有发布镜像？** 在含 `Dockerfile` 的源码根目录执行 `sh start.sh demo`。脚本从固定摘要的 Web 基础镜像构建，仅将 HTTP 端口绑定到 `127.0.0.1:8765`。本机打开 `http://127.0.0.1:8765`；如在 VPS 上运行，可以通过 `ssh -N -L 8765:127.0.0.1:8765 root@你的VPS` 访问。不要与现有实例共用同一端口或同一模式的数据目录。

**Actions 发布镜像后**，VPS 可用一条命令启动。将示例镜像地址换成自己的实际 GHCR 包名，将 `203.0.113.10` 换成 VPS 公网 IPv4。安全组只向自己的 IP 放行 TCP 8765。

```sh
docker run -d --init --restart unless-stopped --name tdl-forwarder-demo --security-opt no-new-privileges -p 8765:8765 -e RELAY_MODE=demo -e RELAY_VPS_IP=203.0.113.10 -v /root/telegram-forwarder/data:/data ghcr.io/homomimo/telegram-forwarder-bot-webui:1.0.0
```

在可信 SSH 中运行 `docker logs tdl-forwarder-demo`。访问 `https://你的VPS公网IPv4:8765`，核对日志 `HTTPS SHA256:` 与浏览器证书指纹，再以 `admin` 和 `Web password:` 后的密码登录。Demo 不需要 Bot Token，不连接 Telegram，也不会生成可用于真实授权的二维码。请勿分享日志。

## 2. 推荐的 Demo 验收顺序

| 操作 | 预期 |
|---|---|
| 工作台 → 从网页转发 40–46，再点确认 | 记录入口为“网页”。详情有 7 个模拟回执，41–43 与 45–46 是两组独立相册；第二组被模拟原生接口明确拒绝，才切 TDL clone。 |
| 模拟 Bot 范围命令 | 经过 Bot 实际的 `/export_range` 管理命令处理器；记录入口为“Bot”，任务号和目标回执与上一次不同。并非真的向 Telegram 机器人发送消息。 |
| 尝试截断相册 | 起点 42 截断 41–43。整个任务在发送前失败，目标不增加帖子。 |
| 模拟回执丢失 | 47 被标为待核对，不换通道重发；查看任务队列，核对后才手工移除标记。 |
| 工作台设置 → TDL / Telethon 查看扫码步骤 | 显示离线预览，不请求 Telegram 凭据。真实二维码仅在 live 模式提供。 |
| 重启容器并重新登录 | 密码、目标、来源、历史和待核对项仍在。旧浏览器会话失效；已完成任务不重放。 |

**本机 Demo 的频道格式**：「工作台设置 → 更改目标／添加来源」先识别，再核对频道名、Telegram 标记 ID 与裸 ID，确认后才保存。支持 `1234567890`、`-1001234567890`、`https://t.me/c/1234567890/`、私有原帖、`@用户名`、公开频道或公开原帖链接。不同写法的同一 ID 不重复监听；复制链接不会授予权限。邀请、评论和话题链接不作为频道设置。Demo 公开用户名只在离线样例 `@demo_news`、`@demo_source`、`@demo_archive` 中查找；真实模式需要已连接的 Telegram 账号，解析后验证为广播频道。手动任务必须给出具体帖子。Bot 的 `/forwardto`、`/monitor` 和 `/channelname` 支持相同写法；公开用户名需核对后用返回的数字命令确认。Docker 环境变量在联网前读取，只接受数字 ID 或能直接提取 ID 的私有链接。

**频道备注**：在「工作台设置」添加监听源或更改目标时，可以填写频道备注；已配置的频道点「编辑备注」，留空保存即可清除。Bot 管理员也可以发送 `/channelname 1234567890 资料归档` 设置、`/channelname 1234567890 -` 清除，用 `/showto` 或 `/monitor` 查看。备注不会修改 Telegram 频道名，也不用于路由。两个频道可以同名，界面始终保留数字 ID；同一频道兼作监听源和目标时共用一个备注。频道从两个角色都移除后，备注随之移除。旧版 `channels.json` 不用迁移。

**Bot 通知示例**（假设已设置备注；以下 ID 和链接仅为示例）：

```text
✅ 自动复制成功 · 已发布 2 条消息
来源：演示素材（9876543210）
目标：演示归档（1234567890）
源帖：
https://t.me/c/9876543210/45
目标帖：
https://t.me/c/1234567890/845?single
https://t.me/c/1234567890/846?single
```

手动投递和自动监听采用同一套来源、目标格式。不设备注则只显示数字 ID；公开原帖在数字 ID 尚未解析前先显示 `@用户名`。入队后即使切换默认目标，通知仍显示这条任务原定的频道。只展示已确认的目标链接，缺少回执不会猜测链接。

## 3. 启动真实模式

下面的镜像名与 Demo 一样，须换成已发布的实际包名；若在本机从源码构建，使用 `telegram-forwarder-bot-webui:1.0.0`。先设置 Telegram 应用 `API_ID`、`API_HASH`、BotFather 签发的 `BOT_TOKEN` 和至少一个 `ADMIN_IDS`（**用户 ID，不是频道 ID**；多个用逗号分隔）。`API_ID` / `API_HASH` 是现有 Bot 后端 Telethon 登录需要的，不能拿 Bot Token 替代。**可将 `-e BOT_TOKEN='实际令牌'` 直接写在 `docker run` 中**，但会留在 shell 历史。下面通过 Bash 隐藏输入，让 Docker 从环境取值：

```bash
read -rsp 'Bot Token: ' BOT_TOKEN; echo; export BOT_TOKEN
read -rsp 'API Hash: ' API_HASH; echo; export API_HASH
docker run -d --init --restart unless-stopped --name tdl-forwarder \
  --security-opt no-new-privileges --log-opt max-size=10m --log-opt max-file=3 \
  -p 8766:8765 -v /root/telegram-forwarder/data:/data \
  -e RELAY_MODE=live -e RELAY_VPS_IP=203.0.113.10 \
  -e BOT_TOKEN -e API_HASH -e API_ID='你的应用数字ID' -e ADMIN_IDS='你的Telegram用户ID' \
  ghcr.io/homomimo/telegram-forwarder-bot-webui:1.0.0
unset BOT_TOKEN API_HASH
docker logs tdl-forwarder
```

可信 SSH 核对指纹后，访问 `https://你的VPS公网IPv4:8766`，安全组同样只放行你自己的 IP。依次在“工作台设置”中：

1. 登录 **TDL 转发账号**：手动转发先通过它读取源消息及分组。它须能读源频道；实际执行 Clone 时还须能在目标频道发帖。
2. 需要用户账号监听时，扫描 **Telethon 用户会话**的独立二维码（按需填写应用凭据和两步验证）。Bot Token 不等于这两个登录会话。已保存会话重新授权，验证通过后才替换；失败、取消不抹掉旧会话。
3. 设置默认目标、添加监听源。向 Bot 发送链接或在网页新建任务：两端使用同一队列，网页“转发记录”与 Bot `/flog` 使用同一份投递记录；目标变更不影响已入队任务。不要让旧实例同时监听同一对源/目标频道。
4. 在自己的测试频道实际检查：单帖、两组相册、`?single`、禁转相册只出现一次、重启后不重复、自动 Clone 的编辑只更新原目标帖。手动转发不建立自动编辑映射。

**发送规则：** 能确定原生发布账号时优先原生；明确禁转才将当前单帖或整组相册切给带逐条回执的 TDL Clone。如果发送前没有可用的原生账号，可直接选择已授权的 TDL。网络超时、回执缺失、未知结果不会被当作禁转；保留待核对，不自动重发。范围按 ID 升序，每次最多 500 个 ID；用分组元数据核对相册，无法安全确认时拒绝发送。监听不补扫离线事件。只处理拥有复制授权的内容。

## 4. 持久化、日志与恢复

| VPS 路径（容器内是 `/data`） | 内容 |
|---|---|
| `/root/telegram-forwarder/data/demo/` | 离线模拟的设置、历史、队列、密码和可选证书；与 live 分开。 |
| `/root/telegram-forwarder/data/live/` | `channels.json`、`message_map.sqlite3`（消息映射）、`message_map.sqlite3.queue.sqlite3`（任务与幂等回执）、`forward_history.json`（投递历史）。 |
| `live/accounts/`、`live/receipts/` | TDL 与 Telethon 会话、Clone 请求和逐条回执；请视作敏感数据。 |
| 每个模式的 `tls/`、`web-password.json` | HTTPS 私钥与证书、首次启动生成的管理员密码；容器重建不会重新生成。 |

容器只初始化指定的专用绑定目录，随后以 UID/GID 10001 运行。不要把整个 `/root` 或 Docker socket 挂进去。若 Docker 使用 rootless、userns-remap 或特殊存储驱动，需确保容器 UID 10001 对挂载目录有读写权限。每份数据只运行一个同模式实例。

默认每次启动在标准输出打印 **Web 密码**，不打印 Bot Token、扫码令牌、两步验证码或 Telegram 会话。用 `-e RELAY_LOG_PASSWORD=0` 可关闭密码日志；`-e RELAY_PASSWORD=...` 可覆盖生成值（至少 16 个字符，只有设置了该环境变量时才生效）。Docker 管理员仍可通过 `docker inspect` 查看容器环境。请限制 SSH、Docker、日志和备份的访问权限。

备份时等投递结束、关闭扫码对话框，**停止所有使用该目录的容器**，完整备份 `/root/telegram-forwarder/data`，包括数据库旁文件、账号和证书；单独保管真实模式的启动凭据。恢复要用新空目录，保留所有权和权限，重新启动前检查源/目标及待核对项。旧 Bot/Web 的任务库与会话不会自动合并，原实例的数据保持不动。

## 5. 传到 GitHub，用 Actions 构建镜像

1. 创建自己的空仓库。解压 GitHub 源码包，把 **`Dockerfile`、`VERSION`、`.github/` 所在这一层**作为 Git 仓库根目录（包含隐藏文件）。不要上传本机 Demo、数据目录、日志、`.env` 或离线镜像包。
2. 将源码提交并推送。分支推送、PR 和手动 **Run workflow** 只验证：检查 Web JavaScript，构建原生 amd64 镜像，在该镜像内运行单测、检查内置 TDL，再用无外网、无宿主数据卷的临时容器验收两种入口和相册回执。不推送包，也不需要 Telegram 凭据。
3. 分支检查通过后，从准备发布的提交打标签。标签必须是 `v` 加上 `VERSION` 的完整内容：

   ```sh
   git tag v1.0.0
   git push origin v1.0.0
   ```

4. 标签工作流会再次验证；通过后以仓库的 `GITHUB_TOKEN` 发布 `linux/amd64` 和 `linux/arm64` 到 `ghcr.io/<小写所有者>/<小写仓库名>`。仅发布 job 申请 `packages: write`，无需 PAT、Bot Token 或 API Hash。组织策略需允许 Actions 写 Packages；要让 VPS 免登录拉取，首次发布后请到 **Packages → Package settings** 确认可见性为 **Public**。私有包需先在 VPS 上登录 GHCR。

从 `1.0.0` 开始，每次发布只给同一个多架构镜像打两个标签：`ghcr.io/homomimo/telegram-forwarder-bot-webui:1.0.0`（固定该版本）和 `ghcr.io/homomimo/telegram-forwarder-bot-webui:latest`（跟随新版本）。`v` 只用于 Git 标签，不用于镜像标签。下次将 `VERSION` 改为 `1.0.1` 并提交，再推送 `v1.0.1`，就会发布 `:1.0.1` 并更新 `:latest`；已有的 `:1.0.0` 不变。生产部署可固定版本号；请勿移动或复用已发布的 Git 标签。

Actions 只接受与 `VERSION` 匹配、从 `1.0.0` 起的正式 `vX.Y.Z` 标签，不生成预览标签或 SHA 标签。请按版本顺序逐个发布；如果仓库已有更高的正式标签，重跑旧版本不会把 `latest` 降级。发布摘要会列出两个标签和镜像清单摘要。

**验证边界：** 此版在本机通过离线单测、HTTP Demo、浏览器和保留数据重启验收。Docker 和真实 Telegram 投递请分别以仓库 Actions 成功记录及自有测试频道回执为准；先在自己的测试频道完成验收，不要直接替换生产环境。

源码自测：安装 `requirements.txt` 后执行 `python -m unittest discover -s tests -q`；在有 Docker 的 Linux 环境也可以从仓库根目录执行 `bash .github/scripts/verify-image.sh` 运行与 CI 相同的镜像验收。

## 6. 上游来源

Bot 基线为 `Homomimo/telegram-tdl-forwarder@7b9ebda`；Web、扫码和 TDL 基线为 `Homomimo/telegram-forwarder-web@bed8124`。集成镜像继承固定摘要的 Web 基础镜像，而不是在构建时下载另一个最新版 TDL。扩展 TDL 的对应源码、补丁和 AGPL-3.0 许可证保留在镜像 `/usr/share/doc/tdl/`；本仓库的 `tdl_patch/` 同时保留上游源码归档与补丁。`BOT-UPSTREAM.md` 为原 Bot 的参考文档，集成版部署以本说明为准。
