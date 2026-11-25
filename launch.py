"""One process owns the Bot, task queue, web API and mounted credentials."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import secrets
import signal
import ssl
import hashlib
import sys
import threading
from http.server import ThreadingHTTPServer

import accounts
from console_http import Console, Handler
import vps_https


def prepare_data(mode):
    base = Path(os.getenv("RELAY_DATA", "/data")).resolve()
    base.mkdir(parents=True, exist_ok=True)
    data = base / mode
    if data.is_symlink():
        raise ValueError("数据目录不能是符号链接")
    data.mkdir(mode=0o700, exist_ok=True)
    # Docker creates a new bind mount as root. Keep private data readable after dropping privileges.
    if sys.platform == "linux" and os.getuid() == 0 and base == Path("/data"):
        os.chown(base, 10001, 10001)  # Support a host bind mount with mode 0700.
        os.chown(data, 10001, 10001)
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    return data


def lock_instance(data):
    lock = (data / ".instance.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise SystemExit("同一数据目录已有实例在运行") from None
    return lock


async def refresh_account(bot, path):
    # start_user_monitor reads the saved credentials. A replacement only takes effect
    # between tasks; do not disconnect an account while it is publishing an album.
    def stamp():
        return path.stat().st_mtime_ns if path.exists() else None

    previous = stamp()
    while True:
        await asyncio.sleep(1)
        current = stamp()
        if (
            current == previous
            or bot._startup_phase
            or not accounts.runtime_lock.acquire(False)
        ):
            continue
        try:
            if bot._user_monitor_watch:
                bot._user_monitor_watch.cancel()
                await asyncio.gather(bot._user_monitor_watch, return_exceptions=True)
                bot._user_monitor_watch = None
            bot.user_monitor_running = False
            if bot.user_client:
                await bot._disconnect_user_monitor(bot.user_client)
            await bot.start_user_monitor()
            previous = current
        finally:
            accounts.runtime_lock.release()


async def serve(mode, data):
    if mode == "demo":
        from demo_transport import DemoTransport

        # Even if someone passes a real token, the demo does not load or use it.
        os.environ.update(
            API_ID="1",
            API_HASH="0" * 32,
            ADMIN_IDS="1",
            BOT_TOKEN="",
            FORWARD_TO_CHAT_ID="1234567890",
            USER_SESSION_STRING="",
            MONITOR_CHAT_IDS="",
        )
    elif not all(
        os.environ.get(k, "").strip()
        for k in ("BOT_TOKEN", "ADMIN_IDS", "API_ID", "API_HASH")
    ):
        raise SystemExit("真实模式需要 BOT_TOKEN、ADMIN_IDS、API_ID、API_HASH。")
    os.environ.update(
        MONITOR_MAP_FILE=str(data / "message_map.sqlite3"),
        FORWARD_LOG_FILE=str(data / "forward_history.json"),
        HOME=str(accounts.tdl_home(data)),
        MONITOR_TDL_FALLBACK="1",
    )
    spec = importlib.util.spec_from_file_location(
        "tdl_forward", Path(__file__).with_name("tdl-forward.py")
    )
    core = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = core
    spec.loader.exec_module(core)
    if mode == "live" and (
        not core.ADMIN_IDS
        or core.API_ID <= 0
        or not core.re.fullmatch("[a-fA-F0-9]{32}", core.API_HASH)
    ):
        raise SystemExit("ADMIN_IDS、API_ID 或 API_HASH 配置无效。")
    core.logging.basicConfig(
        level=core.logging.INFO, format="%(levelname)s %(message)s"
    )
    core.logging.getLogger("telethon").setLevel(core.logging.WARNING)
    password_file = data / "web-password.json"
    password = os.environ.get("RELAY_PASSWORD")
    if password is None:
        if not password_file.exists():
            accounts.private_write(password_file, secrets.token_urlsafe(24))
        password = json.loads(password_file.read_text("utf-8"))
    if (
        not isinstance(password, str)
        or len(password) < 16
        or not password.isprintable()
    ):
        raise SystemExit("网页密码至少 16 个可打印字符")
    bot = core.TelegramBot()
    bot.console_data = data
    cfg = data / "channels.json"
    if cfg.exists():
        saved = json.loads(cfg.read_text("utf-8"))

        def saved_id(raw):
            cid = core.numeric_channel(str(raw))
            if cid is None:
                raise ValueError("已保存的频道 ID 无效，未覆盖原配置")
            return cid

        bot.target_chat_id = (
            saved_id(saved["target"]) if saved["target"] is not None else None
        )
        bot.monitor_chat_ids = list(
            dict.fromkeys(saved_id(cid) for cid in saved["sources"])
        )
        bot.channel_names = {
            saved_id(cid): name for cid, name in saved.get("names", {}).items()
        }
    elif mode == "demo":
        source = DemoTransport.source
        bot.update_channels(
            sources=[source], names={source: "演示素材", bot.target_chat_id: "演示归档"}
        )
    else:
        bot.update_channels()
    if mode == "demo":
        bot.demo_transport = bot.client = DemoTransport(bot.task_queue)
    addr = (os.getenv("BIND", "127.0.0.1"), int(os.getenv("PORT", "8765")))
    ip = os.getenv("RELAY_VPS_IP")
    if ip:
        ip = vps_https.validate_ip(ip, "live", password)
        server = vps_https.IPHTTPSServer(
            addr, Handler, ip, vps_https.tls_context(data, ip)
        )
        cert = (data / "tls" / ip / "cert.pem").read_text("ascii")
        print(
            "HTTPS SHA256:",
            hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert)).hexdigest(),
            flush=True,
        )
    elif mode == "live":
        raise SystemExit("真实模式必须填写 RELAY_VPS_IP 并使用 HTTPS")
    else:
        server = ThreadingHTTPServer(addr, Handler)
    server.console, server.password = Console(bot, core, mode, data), password
    print(f"[{mode}] Web 管理员: admin", flush=True)
    if os.getenv("RELAY_LOG_PASSWORD", "1") == "1":
        print("Web password:", password, flush=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    tasks = [asyncio.create_task(stop.wait())]
    try:
        if mode == "demo":
            await bot.task_queue.start()
        else:
            tasks.extend(
                [
                    asyncio.create_task(
                        refresh_account(bot, data / "accounts" / "telethon.json")
                    ),
                    asyncio.create_task(bot.start()),
                ]
            )
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        bot.begin_shutdown()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.stop()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
    return bot.restart_requested


if __name__ == "__main__":
    os.umask(0o077)
    mode = os.getenv("RELAY_MODE", "demo")
    if mode not in ("demo", "live"):
        raise SystemExit("RELAY_MODE 必须为 demo 或 live")
    if mode == "live" and not Path("/usr/local/bin/tdl").is_file():
        raise SystemExit("需要完整的 live 镜像；Demo 镜像不可用于发送")
    data = prepare_data(mode)
    with lock_instance(data):
        try:
            restart = asyncio.run(serve(mode, data))
        except Exception as exc:
            raise SystemExit(
                f"启动或运行失败（{type(exc).__name__}），请检查凭据、数据权限、端口与网络。"
            ) from None
    if restart:
        os.execv(sys.executable, [sys.executable, *sys.argv])
