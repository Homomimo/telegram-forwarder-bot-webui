"""Authenticated HTTP adapter; all accesses to Bot state run on its asyncio loop."""

import asyncio
import hmac
import json
import os
import re
import sqlite3
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
import accounts
from channel_input import numeric_channel, parse_reference
import web_sessions

ROOT = Path(__file__).resolve().parent
VERSION = (ROOT / "VERSION").read_text("ascii").strip()
SESSIONS = web_sessions.WebSessions()


def check(ok, message):
    if not ok:
        raise ValueError(message)


class Console:
    def __init__(self, bot, core, mode, data):
        self.bot, self.core, self.mode, self.data = bot, core, mode, data
        self.loop = asyncio.get_running_loop()

    def dispatch(self, op, payload=None):
        return asyncio.run_coroutine_threadsafe(
            self.command(op, payload or {}), self.loop
        ).result(15)

    async def command(self, op, p):
        bot, queue = self.bot, self.bot.task_queue
        if op == "state":
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
                for row in reversed(history)
            ]
            total, reviews = queue.journal.reviews(limit=100)
            return dict(
                mode=self.mode,
                version=VERSION,
                config={"target": str(bot.target_chat_id or "")},
                channel_names={
                    str(cid): name
                    for cid in (*bot.monitor_chat_ids, bot.target_chat_id)
                    if cid is not None
                    and (
                        name := bot.channel_names.get(bot.normalize_source_chat_id(cid))
                    )
                },
                queue=queue.snapshot(limit=60),
                reviews=[{"id": r["job_id"], "link": r["link"]} for r in reviews],
                review_total=total,
                history=recent,
                sources=bot.monitor_chat_ids,
                accounts=accounts.summary(self.data) if self.mode == "live" else {},
                listener_status="用户会话已连接"
                if bot.is_user_monitor_active()
                else "仅 Bot / 用户会话未连接",
            )
        check(queue.running and not bot._stopping, "Bot 队列尚未就绪")
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
                self.mode == "demo" or accounts.summary(self.data)["tdl"],
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
        elif op == "demo-bot" and self.mode == "demo":
            # Exercise the real Bot command handler, without a Telegram connection.
            async def reply(*args, **kwargs):
                pass

            event = SimpleNamespace(
                sender_id=self.core.ADMIN_IDS[0],
                chat_id=self.core.ADMIN_IDS[0],
                respond=reply,
            )
            await bot.run_admin_action(
                event, ["/export_range", "9876543210", "40", "46"], reply
            )
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
        host = authority.split(":")[0].lower()
        vps_ip = getattr(self.server, "vps_ip", None)
        if vps_ip:
            valid_host = (
                re.fullmatch(re.escape(vps_ip) + r"(?::[0-9]{1,5})?", authority)
                is not None
            )
        else:
            valid_host = host in {
                "localhost",
                "127.0.0.1",
                os.getenv("RELAY_HOST", "localhost").lower(),
            }
        if len(self.headers.get_all("Host", [])) != 1 or not valid_host:
            self.reply(403, {"error": "不允许的 Host"})
            return False
        return True

    def open_demo(self):
        return False

    def token(self):
        return web_sessions.cookie_token(self.headers.get("Cookie"))

    def session(self):
        return SESSIONS.csrf(self.token())

    def make_cookie(self, token, age=None):
        value = f"{web_sessions.COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"
        if getattr(self.server, "vps_ip", None):
            value += "; Secure"
        return value if age is None else value + f"; Max-Age={age}"

    def do_GET(self):
        if not self.allowed():
            return
        path = urlsplit(self.path).path
        if path == "/api/auth/session":
            csrf = self.session()
            return self.reply(
                200,
                {
                    "authenticated": self.open_demo() or bool(csrf),
                    "open_demo": self.open_demo(),
                    "csrf": csrf,
                },
            )
        if (
            (path.startswith("/api/") or path == "/health")
            and not self.open_demo()
            and not self.session()
        ):
            return self.reply(401, {"error": "请先登录工作台"})
        if path == "/api/state":
            try:
                return self.reply(200, self.server.console.dispatch("state"))
            except (TimeoutError, sqlite3.Error, OSError):
                return self.reply(503, {"error": "队列暂不可用"})
        if path == "/health":
            return self.reply(
                200, {"mode": self.server.console.mode, "version": VERSION}
            )
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
        same_origin = True
        if origin:
            if getattr(self.server, "vps_ip", None):
                same_origin = origin == "https://" + self.headers["Host"]
            else:
                same_origin = origin == "http://" + self.headers["Host"]
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
            if self.path == "/api/auth/login":
                check(
                    isinstance(p.get("username"), str)
                    and isinstance(p.get("password"), str),
                    "请输入账号和密码",
                )
                status, token, csrf = SESSIONS.login(
                    self.client_address[0],
                    p["username"],
                    p["password"],
                    self.server.password,
                )
                if status != 200:
                    return self.reply(
                        status,
                        {
                            "error": "尝试过多，请 5 分钟后再试"
                            if status == 429
                            else "账号或密码不正确"
                        },
                    )
                return self.reply(
                    200,
                    {"csrf": csrf},
                    cookie=self.make_cookie(
                        token, web_sessions.TTL if p.get("remember") is True else None
                    ),
                )
            if not self.open_demo():
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
                check(
                    self.server.console.mode == "live"
                    and len(self.server.password) >= 16,
                    "离线演示不会请求 Telegram 授权，请以真实模式部署",
                )
                check(
                    getattr(self.server, "vps_ip", None)
                    or self.headers["Host"].split(":")[0] in ("localhost", "127.0.0.1"),
                    "请通过配置的 VPS HTTPS 地址或本机入口登录",
                )
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
            self.reply(
                200, result if self.path == "/api/preview-channel" else {"id": result}
            )
        except (TimeoutError, sqlite3.Error, OSError):
            self.reply(503, {"error": "服务忙或存储不可用，请刷新核对状态"})
        except (ValueError, KeyError, TypeError) as e:
            self.reply(400, {"error": str(e)})
