"""Administrator-only, short-lived QR login flows. No terminal transcripts or secret exports."""

import asyncio
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path


# A running send must finish before credentials change or its client disconnects.
runtime_lock = threading.Lock()


def tdl_home(data):
    return Path(data) / "accounts" / "tdl-home"


def summary(data):
    root = Path(data) / "accounts"
    return {
        "tdl": (tdl_home(data) / ".tdl").is_dir(),
        "telethon": (root / "telethon.json").is_file(),
    }


def private_write(path, payload):
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


class QRFrame:
    """Decode skip2/go-qrcode ToSmallString(false) (dark modules are terminal background)."""

    def __init__(self):
        self.rows = []

    def line(self, line):
        if not (
            29 <= len(line) <= 185
            and len(line) % 4 == 1
            and re.fullmatch("[ ▄▀█]+", line)
        ):
            self.rows.clear()
            return
        if self.rows and len(line) != len(self.rows[0]):
            self.rows.clear()
        self.rows.append(line)
        if len(self.rows) != (len(line) + 1) // 2:
            return
        bits = []
        for row in self.rows:
            bits.extend(
                [
                    "".join("1" if c in chars else "0" for c in row)
                    for chars in (" ▄", " ▀")
                ]
            )
        self.rows.clear()
        return bits[: len(line)]


def replace_tdl_session(source, home):
    home.mkdir(mode=0o700, exist_ok=True)
    current = home / ".tdl"
    backup = home / (".tdl-previous-" + secrets.token_hex(8))
    if current.exists():
        current.rename(backup)
    try:
        source.rename(current)
    except OSError:
        if backup.exists():
            backup.rename(current)
        raise
    # Keep any undeletable backup outside the login staging directory for recovery.
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)


async def tdl_login(f, root, binary, _):
    import pexpect

    home = tdl_home(root.parent)
    with tempfile.TemporaryDirectory(dir=root) as stage:
        child = pexpect.spawn(
            binary,
            ["login", "-T", "qr"],
            env={**os.environ, "HOME": stage, "NO_COLOR": "1", "TERM": "xterm"},
            encoding="utf-8",
            codec_errors="replace",
            dimensions=(160, 240),
            echo=False,
        )
        pending = ""
        frame = QRFrame()
        success = False
        tail = ""
        asked = False
        try:
            while True:
                try:
                    chunk = child.read_nonblocking(8192, 0.05)
                except pexpect.TIMEOUT:
                    chunk = ""
                except pexpect.EOF:
                    break
                pending += chunk
                if "\x1b[6n" in pending:
                    child.send("\x1b[1;1R")
                    pending = pending.replace("\x1b[6n", "")
                while "\n" in pending:
                    ln, pending = pending.split("\n", 1)
                    cleaned = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", ln).rstrip("\r")
                    matrix = frame.line(cleaned)
                    if matrix and not asked:
                        f.show("scan", matrix)
                tail = (tail + chunk)[-2048:]
                if not asked and "Enter 2FA Password:" in tail:
                    asked = True
                    f.show("password")
                if asked and f.password is not None:
                    pwd = f.password
                    f.password = None
                    child.send(pwd + "\r")
                    f.show("checking")
                if "Login successfully!" in tail:
                    success = True
                pending = pending[-16000:]
                await asyncio.sleep(0.06)
            child.close()
            if not success or child.exitstatus != 0:
                raise ValueError(
                    "TDL 登录失败，请检查网络、扫码确认或两步验证密码后重试"
                )
            source = Path(stage) / ".tdl"
            if not source.is_dir():
                raise ValueError("TDL 未生成有效会话")
            await f.commit(lambda: replace_tdl_session(source, home))
        finally:
            if child.isalive():
                child.close(force=True)


async def telethon_login(f, root, _, credentials):
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession
    import segno

    api_id, api_hash = credentials
    client = TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        device_model="Telegram Forwarder Web / Telethon",
        connection_retries=1,
        request_retries=1,
    )
    try:
        await client.connect()
        while True:
            qr = await client.qr_login()
            code = segno.make(qr.url, micro=False, error="m")
            matrix = [
                "".join("1" if x else "0" for x in row)
                for row in code.matrix_iter(border=4)
            ]
            f.show("scan", matrix)
            try:
                await qr.wait()
                break
            except asyncio.TimeoutError:
                continue
            except errors.SessionPasswordNeededError:
                f.show("password")
                while f.password is None:
                    await asyncio.sleep(0.1)
                pwd = f.password
                f.password = None
                f.show("checking")
                await client.sign_in(password=pwd)
                break
        if not await client.is_user_authorized():
            raise ValueError("账号未完成授权")
        saved = dict(api_id=api_id, api_hash=api_hash, session=client.session.save())
    finally:
        await client.disconnect()
    # Publish atomically only after the login client releases the authorization.
    await f.commit(lambda: private_write(root / "telethon.json", saved))


class LoginStopped(Exception):
    """The flow was cancelled or expired before publishing new credentials."""


class Flow:
    def __init__(self):
        self.id = secrets.token_urlsafe(24)
        self.view = ("starting", None)
        self.password = None
        self.cancelled = False
        self.active = True
        self.error = ""
        self.deadline = time.monotonic() + 180
        self.lock = threading.Lock()
        self.committed = False

    def show(self, phase, qr=None):
        self.view = (phase, qr)

    def check_active(self):
        if self.cancelled or time.monotonic() >= self.deadline:
            raise LoginStopped

    async def commit(self, publish):
        self.show("saving")
        while not runtime_lock.acquire(blocking=False):
            self.check_active()
            await asyncio.sleep(0.1)
        try:
            # Cancellation and publication have one ordering, even on different threads.
            with self.lock:
                self.check_active()
                publish()
                self.committed = True
        finally:
            runtime_lock.release()


class Manager:
    def __init__(self):
        self.lock = threading.Lock()
        self.flow = None

    def command(self, op, p, data, bin):
        with self.lock:
            f = self.flow
            if op == "start":
                provider = p.get("provider")
                if provider not in ("tdl", "telethon"):
                    raise ValueError("请选择登录类型")
                if f and f.active:
                    raise ValueError("已有登录正在进行，请关闭或等待其过期")
                saved = summary(data)[provider]
                if saved and p.get("replace") is not True:
                    raise ValueError("已保存此类会话，请通过重新扫码登录确认替换")
                creds = None
                if provider == "telethon":
                    aid = str(p.get("api_id", "")).strip()
                    ah = str(p.get("api_hash", "")).strip()
                    if bool(aid) != bool(ah):
                        raise ValueError("自定义应用凭据时，请同时填写 API ID 和 API Hash")
                    if not saved and not aid:
                        aid = os.environ.get("API_ID", "").strip()
                        ah = os.environ.get("API_HASH", "").strip()
                    elif saved and not aid:
                        try:
                            stored = json.loads(
                                (Path(data) / "accounts" / "telethon.json").read_text("utf-8")
                            )
                            aid = str(stored["api_id"])
                            ah = str(stored["api_hash"])
                        except (OSError, ValueError, KeyError, TypeError):
                            raise ValueError("无法读取原应用凭据，请填写 API ID 和 API Hash") from None
                    if (
                        not re.fullmatch("[1-9][0-9]{0,9}", aid)
                        or int(aid) >= 2**31
                        or not re.fullmatch("[A-Fa-f0-9]{32}", ah)
                    ):
                        raise ValueError("请输入有效的 API ID 和 32 位 API Hash")
                    creds = (int(aid), ah)
                root = Path(data) / "accounts"
                root.mkdir(mode=0o700, exist_ok=True)
                self.flow = f = Flow()
                threading.Thread(
                    target=self.run, args=(f, root, bin, creds, provider), daemon=True
                ).start()
                return {"flow": f.id}
            if not f or not secrets.compare_digest(
                f.id.encode(), str(p.get("flow", "")).encode()
            ):
                raise ValueError("登录已结束，请重新开始")
            if op == "cancel":
                with f.lock:
                    if f.active and not f.committed:
                        f.cancelled = True
                        f.password = None
                        f.show("cancelled")
                return {}
            if op == "password":
                pwd = p.get("password")
                if (
                    f.view[0] != "password"
                    or f.cancelled
                    or not isinstance(pwd, str)
                    or not 0 < len(pwd) <= 256
                    or any(ord(c) < 32 or ord(c) == 127 for c in pwd)
                ):
                    raise ValueError("当前无法提交密码")
                f.password = pwd
                f.show("checking")
                return {}
            if op != "poll":
                raise ValueError("未知登录操作")
            phase, matrix = f.view
            return dict(
                phase=phase,
                qr=matrix,
                error=f.error,
                remaining=max(0, int(f.deadline - time.monotonic())),
            )

    def run(self, f, root, bin, creds, provider):
        async def supervise():
            task = asyncio.create_task(
                (tdl_login if provider == "tdl" else telethon_login)(
                    f, root, bin, creds
                )
            )
            try:
                while not task.done():
                    if f.cancelled or time.monotonic() >= f.deadline:
                        return "cancelled" if f.cancelled else "expired"
                    await asyncio.sleep(0.15)
                await task
                return "done"
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        try:
            phase = asyncio.run(supervise())
        except LoginStopped:
            phase = "cancelled" if f.cancelled else "expired"
        except Exception:
            phase = "failed"
            f.error = (
                "登录未完成，请检查 Telegram 网络、应用凭据及两步验证密码，再重新扫码。"
            )
        finally:
            with self.lock:
                f.password = None
                f.show(phase)
                f.active = False


manager = Manager()
