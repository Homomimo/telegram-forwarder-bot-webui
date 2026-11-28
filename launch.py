"""One process owns the Bot, task queue, web API and mounted credentials."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import signal
import ssl
import hashlib
import sys
import threading

import accounts
from console_http import Console, Handler
from file_staging import FileStaging, mounted_downloads
import vps_https


def prepare_data():
    data = Path(os.getenv("RELAY_DATA", "/data")).resolve()
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    legacy = data / "live"
    if legacy.exists() or legacy.is_symlink():
        raise SystemExit("发现旧数据目录 /data/live。请先迁移数据到 /data，未启动服务。")
    # The bind mount itself now stores private data. Restrict it before dropping privileges.
    if sys.platform == "linux" and os.getuid() == 0 and data == Path("/data"):
        data.chmod(0o700)
        os.chown(data, 10001, 10001)
        if downloads := mounted_downloads():
            downloads.chmod(0o700)
            os.chown(downloads, 10001, 10001)
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


async def serve(data):
    if not all(
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
    if (
        not core.ADMIN_IDS
        or core.API_ID <= 0
        or not core.re.fullmatch("[a-fA-F0-9]{32}", core.API_HASH)
    ):
        raise SystemExit("ADMIN_IDS、API_ID 或 API_HASH 配置无效。")
    core.logging.basicConfig(
        level=core.logging.INFO, format="%(levelname)s %(message)s"
    )
    core.logging.getLogger("telethon").setLevel(core.logging.WARNING)
    try:
        ip, ip_source = vps_https.resolve_ip(os.getenv("RELAY_VPS_IP"))
    except ValueError as exc:
        raise SystemExit(f"[启动/WebUI] {exc}") from None
    bot = core.TelegramBot()
    bot.console_data = data
    if downloads := mounted_downloads():
        if not os.access(downloads, os.R_OK | os.W_OK | os.X_OK):
            raise SystemExit("/downloads 必须是 Bot 可读写的独立挂载目录")
        bot.file_staging = FileStaging(bot, data, downloads)
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
        bot.load_channel_pairs(saved)
    else:
        bot.update_channels()
    addr = (os.getenv("BIND", "127.0.0.1"), int(os.getenv("PORT", "8765")))
    try:
        public_port = int(os.environ.get("WEB_PORT", addr[1]))
    except ValueError:
        raise SystemExit("WEB_PORT 必须是 1 到 65535 之间的整数") from None
    if not 1 <= public_port <= 65535:
        raise SystemExit("WEB_PORT 必须是 1 到 65535 之间的整数")
    bot.admin_panel.webui_url = f"https://{ip}:{public_port}"
    server = vps_https.IPHTTPSServer(addr, Handler, ip, vps_https.tls_context(data, ip))
    cert = (data / "tls" / ip / "cert.pem").read_text("ascii")
    accounts.private_write(vps_https.RUNTIME_IP_FILE, ip)
    print(f"[启动/WebUI] {bot.admin_panel.webui_url}（{ip_source}）", flush=True)
    print("HTTPS SHA256:", hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert)).hexdigest(), flush=True)
    server.console = Console(bot, core, data)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    tasks = [
        asyncio.create_task(stop.wait()),
        asyncio.create_task(refresh_account(bot, data / "accounts" / "telethon.json")),
        asyncio.create_task(bot.start()),
    ]
    try:
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
    if os.getenv("RELAY_MODE", "live") != "live":
        raise SystemExit("RELAY_MODE 只支持 live，未启动服务")
    if not Path("/usr/local/bin/tdl").is_file():
        raise SystemExit("镜像缺少 /usr/local/bin/tdl")
    data = prepare_data()
    with lock_instance(data):
        try:
            restart = asyncio.run(serve(data))
        except Exception as exc:
            raise SystemExit(
                f"启动或运行失败（{type(exc).__name__}），请检查凭据、数据权限、端口与网络。"
            ) from None
    if restart:
        os.execv(sys.executable, [sys.executable, *sys.argv])
