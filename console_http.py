"""Authenticated HTTP adapter; all accesses to Bot state run on its asyncio loop."""

import asyncio
import hmac
import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import accounts
from channel_input import numeric_channel, parse_reference
from forward_records import post_channel_url, post_url
import web_sessions

ROOT = Path(__file__).resolve().parent
VERSION = (ROOT / "VERSION").read_text("ascii").strip()
SESSIONS = web_sessions.SESSIONS


def check(ok, message):
    if not ok:
        raise ValueError(message)


class Console:
    def __init__(self, bot, core, data):
        self.bot, self.core, self.data = bot, core, data
        self.loop = asyncio.get_running_loop()

    def dispatch(self, op, payload=None):
        return asyncio.run_coroutine_threadsafe(
            self.command(op, payload or {}), self.loop
        ).result(15)

    async def command(self, op, p):
        bot, queue = self.bot, self.bot.task_queue
        if op == "mappings":
            page = p.get("page", "1")
            check(isinstance(page, str) and re.fullmatch(r"[1-9][0-9]{0,8}", page), "页码必须是正整数")
            return await bot.list_monitor_mappings(int(page))
        if op == "state":
            channel_ids = {*bot.monitor_chat_ids, bot.target_chat_id, *bot.channel_pairs}
            channel_ids.update(pair[0] for pair in bot.channel_pairs.values())
            history = bot.forward_log.get_recent(100)
            recent = [
                {
                    k: row[k]
                    for k in (
                        "id",
                        "job_id",
                        "origin",
                        "from",
                        "to",
                        "kind",
                        "route",
                        "members",
                        "timestamp",
                        "album",
                        "album_groups",
                        "success",
                        "unconfirmed",
                        "error",
                    )
                    if k in row
                }
                | bot.history_channel_labels(row)
                | {
                    "target_post_base": post_channel_url(row["to"]),
                    "legacy_target_url": post_url(row["to"], row.get("target_message_id"), row.get("album"))
                    if row.get("success") else None,
                }
                for row in reversed(history)
            ]
            total, reviews = queue.journal.reviews(limit=100)
            return dict(
                mode="live",
                version=VERSION,
                config={"target": str(bot.target_chat_id or "")},
                channel_names={
                    str(cid): name
                    for cid in channel_ids
                    if cid is not None
                    and (
                        name := bot.channel_names.get(bot.normalize_source_chat_id(cid))
                    )
                },
                queue=queue.snapshot(limit=60),
                reviews=[
                    dict(id=r["job_id"], link=r["link"],
                         source_label=bot.source_channel_label(r["link"], r.get("source_chat_id")),
                         target_label=bot.channel_label(r["target_chat_id"]))
                    for r in reviews
                ],
                review_total=total,
                history=recent,
                sources=bot.monitor_chat_ids,
                pairing=bot.pairing_state(),
                accounts=accounts.summary(self.data),
                listener_status="用户会话已连接"
                if bot.is_user_monitor_active()
                else "仅 Bot / 用户会话未连接",
            )
        check(queue.running and not bot._stopping, "Bot 队列尚未就绪")
        if op == "channel-pair":
            action = p.get("action")
            check(action in ("add", "edit", "toggle", "delete", "activate", "deactivate"), "无效的频道配对操作")
            check(p.get("confirmed") is True, "请确认频道配对操作")
            source = numeric_channel(str(p.get("source", "")))
            target = numeric_channel(str(p.get("target", "")))
            if action not in ("activate", "deactivate"):
                check(source, "请选择有效的源频道")
            if action in ("add", "edit"):
                check(target, "请选择有效的目标频道")
            source_title, target_title = p.get("source_title", ""), p.get("target_title", "")
            check(all(isinstance(title, str) and len(title) <= 256 for title in (source_title, target_title)),
                  "识别到的频道名称无效")
            bot.change_channel_pair(action, source, target, expected=p.get("revision"),
                                    source_title=source_title, target_title=target_title)
            return {"ok": True}
        if op == "clear-mappings":
            check(p.get("confirmed") is True, "请确认清理全部源帖映射")
            return {"removed": await bot.clear_monitor_mappings()}
        if op == "preview-channel":
            cid, title = await bot.resolve_channel(p.get("channel", ""))
            return {
                "channel": str(cid),
                "title": title,
                "name": bot.channel_names.get(cid, ""),
            }
        if op == "submit":
            key = p.get("key")
            check(
                isinstance(key, str) and re.fullmatch(r"[a-f0-9-]{32,36}", key),
                "缺少提交标识",
            )
            target = bot.target_chat_id
            check(
                target
                and numeric_channel(str(p.get("target", "")))
                == bot.normalize_source_chat_id(target),
                "目标频道已改变，请重新确认",
            )
            link = p.get("src", "")
            ref = parse_reference(link)
            check(
                ref.post is not None,
                "这里需要具体帖子链接，请粘贴带消息 ID 的原帖链接。",
            )
            check(
                isinstance(link, str)
                and len(link) <= 256
                and bot.extract_link(link) == link,
                "请输入完整有效的源帖链接",
            )
            check(
                accounts.summary(self.data)["tdl"],
                "请先在设置中扫码登录 TDL",
            )
            check(p.get("kind") in ("link", "range"), "未知任务类型")
            opts = {}
            if p["kind"] == "range":
                source = link
                if isinstance(ref.peer, str):
                    resolved, _ = await bot.resolve_channel(link)
                    source = f"https://t.me/c/{resolved}/{ref.post}"
                channel, first, last, err = self.core.parse_export_range_args(
                    ["/export_range", source, str(p.get("end", ""))]
                )
                check(
                    not err and not link.endswith("?single") and last - first < 500,
                    "范围需同源、升序且不超过 500 个 ID",
                )
                opts.update(
                    mode="export_range",
                    message_ids=[first, last],
                    source_chat_id=channel,
                )
            check(target == bot.target_chat_id, "目标频道已改变，请重新确认")
            job_id = await queue.add_task(
                None, link, None, 0, target_chat_id=target, web_key=key, **opts
            )
            check(job_id is not None, queue.admission_error())
            return job_id
        if op == "target":
            target = self.core.parse_chat_id(str(p.get("target", "")))
            check(target, "目标频道 ID 无效")
            if "previous" in p:
                check(
                    p["previous"] == str(bot.target_chat_id or ""),
                    "目标已改变，请刷新重试",
                )
            name = p.get("name")
            bot.update_channels(
                target=target,
                names={target: name} if name is not None and name != "" else None,
            )
        elif op == "channel-name":
            channel = self.core.parse_chat_id(str(p.get("channel", "")))
            check(channel and isinstance(p.get("name"), str), "频道 ID 或备注无效")
            if "previous" in p:
                old_name = bot.channel_names.get(
                    bot.normalize_source_chat_id(channel), ""
                )
                check(old_name == p["previous"], "备注已改变，请刷新重试")
            bot.update_channels(names={channel: p["name"]})
        elif op in ("add-source", "remove-source"):
            channel = self.core.parse_monitor_chat_id(str(p.get("source", "")))
            check(channel, "请填写源频道数字 ID")
            if op == "add-source":
                name = p.get("name")
                changed = bot.add_monitor_chat_id(channel, name if name != "" else None)
            else:
                changed = bot.remove_monitor_chat_id(channel)
            check(
                changed,
                "该频道已在监听列表中"
                if op == "add-source"
                else "该频道不在监听列表中",
            )
        elif op == "resolve":
            changed = queue.journal.resolve_reviews(
                [int(p["id"])], current_id=getattr(queue.current_task, "job_id", None)
            )
            check(changed, "任务不是待核对状态")
        else:
            raise ValueError("不支持此操作")


class Handler(BaseHTTPRequestHandler):
    def log_request(self, code="-", size="-"):
        pass  # Do not log URLs, user input or credentials.

    def reply(self, n, body, mime="application/json; charset=utf-8", cookie=None):
        data = (
            body
            if isinstance(body, bytes)
            else json.dumps(body, ensure_ascii=False).encode()
        )
        self.send_response(n)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'",
        )
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        authority = self.headers.get("Host", "")
        valid_host = re.fullmatch(re.escape(self.server.vps_ip) + r"(?::[0-9]{1,5})?", authority)
        if len(self.headers.get_all("Host", [])) != 1 or not valid_host:
            self.reply(403, {"error": "不允许的 Host"})
            return False
        return True

    def token(self):
        return web_sessions.cookie_token(self.headers.get("Cookie"))

    def session(self):
        return SESSIONS.csrf(self.token(), self.server.console.bot.admin_panel.allowed)

    def make_cookie(self, token, age=None):
        value = f"{web_sessions.COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Secure"
        return value if age is None else value + f"; Max-Age={age}"

    def do_GET(self):
        if not self.allowed():
            return
        url = urlsplit(self.path)
        path = url.path
        if path == "/api/auth/session":
            csrf = self.session()
            return self.reply(
                200,
                {"authenticated": bool(csrf), "csrf": csrf},
            )
        if (path.startswith("/api/") or path == "/health") and not self.session():
            return self.reply(401, {"error": "请先登录工作台"})
        if path == "/api/state":
            try:
                return self.reply(200, self.server.console.dispatch("state"))
            except (TimeoutError, sqlite3.Error, OSError):
                return self.reply(503, {"error": "队列暂不可用"})
        if path == "/api/mappings":
            pages = parse_qs(url.query, keep_blank_values=True).get("page", ["1"])
            if len(pages) != 1:
                return self.reply(400, {"error": "请指定一个页码"})
            try:
                return self.reply(200, self.server.console.dispatch("mappings", {"page": pages[0]}))
            except ValueError as exc:
                return self.reply(400, {"error": str(exc)})
            except (TimeoutError, sqlite3.Error, OSError):
                return self.reply(503, {"error": "同步映射暂不可用"})
        if path == "/health":
            return self.reply(200, {"mode": "live", "version": VERSION})
        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/ui.css": ("ui.css", "text/css"),
            "/app.js": ("app.js", "application/javascript"),
            "/glass.css": ("glass.css", "text/css"),
            "/prism.svg": ("prism.svg", "image/svg+xml"),
            "/login": ("index.html", "text/html; charset=utf-8"),
        }
        if path not in files:
            return self.reply(404, {"error": "页面不存在"})
        f, m = files[path]
        self.reply(200, (ROOT / "static" / f).read_bytes(), m)

    def do_POST(self):
        if not self.allowed():
            return
        origin = self.headers.get("Origin")
        same_origin = not origin or origin == "https://" + self.headers["Host"]
        if (
            self.headers.get("X-Relay-Request") != "1"
            or self.headers.get_content_type() != "application/json"
            or not same_origin
        ):
            return self.reply(403, {"error": "跨站请求已拒绝"})
        if not self.path.startswith("/api/"):
            return self.reply(404, {"error": "未知接口"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            check(0 < n <= 8192, "请求大小不合法")
            p = json.loads(self.rfile.read(n))
            check(isinstance(p, dict), "请求格式不正确")
            if self.path == "/api/auth/bot-link":
                code = p.get("code")
                token, csrf = (SESSIONS.redeem(code, self.server.console.bot.admin_panel.allowed)
                               if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", code)
                               else (None, None))
                if not token:
                    return self.reply(401, {"error": "登录链接已失效，请回 Bot 获取新链接。"})
                return self.reply(200, {"csrf": csrf}, cookie=self.make_cookie(token))
            csrf = self.session()
            if csrf is None:
                return self.reply(401, {"error": "请重新登录工作台"})
            if not hmac.compare_digest(
                csrf.encode(), self.headers.get("X-CSRF-Token", "").encode()
            ):
                return self.reply(403, {"error": "会话校验失败，请刷新页面"})
            if self.path == "/api/auth/logout":
                SESSIONS.revoke(self.token())
                return self.reply(200, {"ok": True}, cookie=self.make_cookie("", 0))
            if self.path.startswith("/api/login/"):
                return self.reply(
                    200,
                    accounts.manager.command(
                        self.path[11:],
                        p,
                        self.server.console.data,
                        self.server.console.bot.tdl.tdl_path,
                    ),
                )
            result = self.server.console.dispatch(self.path[5:], p)
            self.reply(200, result if self.path in ("/api/preview-channel", "/api/clear-mappings", "/api/channel-pair")
                       else {"id": result})
        except (TimeoutError, sqlite3.Error, OSError):
            self.reply(503, {"error": "服务忙或存储不可用，请刷新核对状态"})
        except (ValueError, KeyError, TypeError) as e:
            self.reply(400, {"error": str(e)})
