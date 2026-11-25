import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounts as a


class LoginTest(unittest.TestCase):
    def test_tdl_terminal_qr_preserves_orientation(self):
        # Include all four upper/lower pixel combinations and an odd last row.
        n = 37
        expected = [
            "".join("1" if (i + j * j) % 5 < 2 else "0" for j in range(n))
            for i in range(n)
        ]
        lookup = {"00": "█", "01": "▀", "10": "▄", "11": " "}
        f = a.QRFrame()
        for y in range(0, n, 2):
            other = expected[y + 1] if y + 1 < n else "1" * n
            got = f.line("".join(lookup[x + b] for x, b in zip(expected[y], other)))
        self.assertEqual(got, expected)

    def test_manager_cancel_expiry_and_no_overwrite(self):
        async def wait(*args):
            await asyncio.sleep(600)

        with tempfile.TemporaryDirectory() as td, patch.object(a, "tdl_login", wait):
            root = Path(td)
            m = a.Manager()
            start = lambda: m.command("start", {"provider": "tdl"}, root, "tdl")
            flow = start()
            self.assertRaises(ValueError, start)
            self.assertRaises(
                ValueError, m.command, "poll", {"flow": "wrong"}, root, "tdl"
            )
            self.assertRaises(
                ValueError,
                m.command,
                "password",
                {**flow, "password": "dummy"},
                root,
                "tdl",
            )
            m.command("cancel", flow, root, "tdl")
            self.finish(m)
            self.assertEqual(m.flow.view, ("cancelled", None))
            start()
            m.flow.deadline = 0
            self.finish(m)
            self.assertEqual(m.flow.view, ("expired", None))
            (a.tdl_home(root) / ".tdl").mkdir(parents=True)
            self.assertRaises(ValueError, start)

    @staticmethod
    def finish(m):
        for _ in range(150):
            if not m.flow.active:
                return
            time.sleep(0.02)
        raise AssertionError("flow did not finish")

    def test_telethon_two_factor_saves_only_after_auth(self):
        try:
            from telethon.errors import SessionPasswordNeededError
            import segno
        except ImportError:
            self.skipTest("install the project requirements for adapter tests")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = AsyncMock()
            client.session = Mock()
            client.session.save.return_value = "synthetic-session"
            client.is_user_authorized.return_value = True
            qr = Mock(url="tg://login?token=test")
            qr.wait = AsyncMock(side_effect=SessionPasswordNeededError(None))
            client.qr_login.return_value = qr

            async def run():
                f = a.Flow()
                task = asyncio.create_task(
                    a.telethon_login(f, root, None, (123, "a" * 32))
                )
                await asyncio.sleep(0.03)
                self.assertEqual(f.view, ("password", None))
                self.assertFalse((root / "telethon.json").exists())
                f.password = "synthetic-password"
                await asyncio.wait_for(task, 2)

            with patch("telethon.TelegramClient", return_value=client):
                asyncio.run(run())
            self.assertEqual(
                json.loads((root / "telethon.json").read_text())["session"],
                "synthetic-session",
            )
            client.disconnect.assert_awaited_once()
            client.sign_in.assert_awaited_once_with(password="synthetic-password")

    def test_first_telethon_login_uses_container_credentials_unless_both_are_overridden(self):
        configured = {"API_ID": "123", "API_HASH": "a" * 32}
        with tempfile.TemporaryDirectory() as td, patch.dict(a.os.environ, configured):
            data = Path(td)
            for fields, expected in [
                ({"api_id": "", "api_hash": ""}, (123, "a" * 32)),
                ({"api_id": "456", "api_hash": "b" * 32}, (456, "b" * 32)),
            ]:
                manager = a.Manager()
                with patch.object(a, "telethon_login", new_callable=AsyncMock) as login:
                    response = manager.command("start", {"provider": "telethon", **fields}, data, "tdl")
                    self.finish(manager)
                    self.assertEqual(response, {"flow": manager.flow.id})
                    login.assert_awaited_once_with(manager.flow, data / "accounts", "tdl", expected)

            for partial in ({"api_id": "456"}, {"api_hash": "b" * 32}):
                with self.assertRaisesRegex(ValueError, "同时填写"):
                    a.Manager().command("start", {"provider": "telethon", **partial}, data, "tdl")

    def test_relogin_requires_confirmation_and_reuses_application_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            root = data / "accounts"
            root.mkdir()
            path = root / "telethon.json"
            original = dict(api_id=123, api_hash="a" * 32, session="old-session")
            a.private_write(path, original)
            m = a.Manager()
            for confirm in (None, False, "true", 1):
                self.assertRaises(
                    ValueError, m.command, "start",
                    {"provider": "telethon", "replace": confirm}, data, "tdl",
                )
                self.assertEqual(json.loads(path.read_text()), original)

            received = []

            async def authenticate(f, root, binary, credentials):
                received.append(credentials)
                await f.commit(lambda: a.private_write(path, {**original, "session": "new-session"}))
                # A close request arriving after publication must not turn success into cancellation.
                m.command("cancel", {"flow": f.id}, data, binary)

            configured = {"API_ID": "456", "API_HASH": "b" * 32}
            with patch.dict(a.os.environ, configured), patch.object(a, "telethon_login", authenticate):
                m.command("start", {"provider": "telethon", "replace": True}, data, "tdl")
                self.finish(m)
            self.assertEqual(received, [(123, "a" * 32)])
            self.assertEqual(json.loads(path.read_text())["session"], "new-session")
            self.assertEqual(m.flow.view, ("done", None))
            self.assertFalse(m.flow.cancelled)

    def test_failed_or_abandoned_relogin_keeps_both_original_sessions(self):
        for provider in ("tdl", "telethon"):
            for outcome in ("cancelled", "expired", "failed"):
                with self.subTest(provider=provider, outcome=outcome), tempfile.TemporaryDirectory() as td:
                    data = Path(td)
                    old_tdl = a.tdl_home(data) / ".tdl" / "session"
                    old_tdl.parent.mkdir(parents=True)
                    old_tdl.write_text("original-tdl-session")
                    old_telethon = data / "accounts" / "telethon.json"
                    a.private_write(old_telethon, dict(api_id=123, api_hash="a" * 32, session="original-telethon-session"))
                    original = old_telethon.read_bytes()

                    async def authenticate(*args):
                        if outcome == "failed":
                            raise ValueError("synthetic-private-error")
                        await asyncio.sleep(600)

                    m = a.Manager()
                    with patch.object(a, provider + "_login", authenticate):
                        flow = m.command("start", {"provider": provider, "replace": True}, data, "tdl")
                        if outcome == "cancelled":
                            m.command("cancel", flow, data, "tdl")
                        elif outcome == "expired":
                            m.flow.deadline = 0
                        self.finish(m)
                    self.assertEqual(m.flow.view, (outcome, None))
                    self.assertEqual(old_tdl.read_text(), "original-tdl-session")
                    self.assertEqual(old_telethon.read_bytes(), original)
                    self.assertNotIn("synthetic-private-error", m.flow.error)

    def test_session_commit_waits_for_sends_and_respects_cancel_and_expiry(self):
        async def scenario(outcome):
            f = a.Flow()
            publish = Mock()
            with a.runtime_lock:
                task = asyncio.create_task(f.commit(publish))
                await asyncio.sleep(0.02)
                self.assertEqual(f.view, ("saving", None))
                publish.assert_not_called()
                if outcome == "cancelled":
                    f.cancelled = True
                elif outcome == "expired":
                    f.deadline = 0
            if outcome == "done":
                await asyncio.wait_for(task, 1)
                publish.assert_called_once()
                self.assertTrue(f.committed)
            else:
                with self.assertRaises(a.LoginStopped):
                    await asyncio.wait_for(task, 1)
                publish.assert_not_called()
                self.assertFalse(f.committed)
            self.assertFalse(a.runtime_lock.locked())

        for outcome in ("done", "cancelled", "expired"):
            with self.subTest(outcome=outcome):
                asyncio.run(scenario(outcome))

    def test_tdl_session_replacement_rolls_back_if_installation_fails(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                home = root / "tdl-home"
                current = home / ".tdl"
                current.mkdir(parents=True)
                (current / "session").write_text("old-tdl")
                source = root / "new-session"
                source.mkdir()
                (source / "session").write_text("new-tdl")
                rename = Path.rename

                def move(path, target):
                    if fail and path == source:
                        raise OSError("synthetic installation failure")
                    return rename(path, target)

                with patch.object(Path, "rename", move):
                    if fail:
                        self.assertRaises(OSError, a.replace_tdl_session, source, home)
                    else:
                        a.replace_tdl_session(source, home)
                self.assertEqual((current / "session").read_text(), "old-tdl" if fail else "new-tdl")
                self.assertEqual(list(home.iterdir()), [current])

    def test_tdl_relogin_authenticates_in_isolation_before_replacing_session(self):
        import pexpect

        for outcome in ("done", "failed", "cancelled", "expired"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as td:
                data = Path(td)
                home = a.tdl_home(data)
                current = home / ".tdl"
                current.mkdir(parents=True)
                (current / "session").write_text("old-session")
                f = a.Flow()
                child = Mock()
                child.read_nonblocking.side_effect = ["Login successfully!\r\n", pexpect.EOF("")]
                child.exitstatus = 1 if outcome == "failed" else 0
                child.isalive.return_value = False

                def spawn(binary, arguments, **kwargs):
                    stage = Path(kwargs["env"]["HOME"])
                    self.assertNotEqual(stage, home)
                    self.assertEqual(arguments, ["login", "-T", "qr"])
                    self.assertEqual((current / "session").read_text(), "old-session")
                    (stage / ".tdl").mkdir()
                    (stage / ".tdl" / "session").write_text("new-session")
                    return child

                if outcome == "cancelled":
                    f.cancelled = True
                elif outcome == "expired":
                    f.deadline = 0
                with patch("pexpect.spawn", side_effect=spawn, create=True):
                    login = a.tdl_login(f, data / "accounts", "tdl", None)
                    if outcome == "done":
                        asyncio.run(login)
                    else:
                        expected = ValueError if outcome == "failed" else a.LoginStopped
                        self.assertRaises(expected, asyncio.run, login)
                self.assertEqual(
                    (current / "session").read_text(),
                    "new-session" if outcome == "done" else "old-session",
                )

    def test_telethon_relogin_does_not_publish_after_failed_auth_or_cancel(self):
        for outcome in ("done", "failed", "cancelled", "expired"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                path = root / "telethon.json"
                a.private_write(path, {"session": "old-session"})
                original = path.read_bytes()
                f = a.Flow()
                client = AsyncMock()
                client.session = Mock()
                client.session.save.return_value = "new-session"
                client.is_user_authorized.return_value = outcome != "failed"
                client.qr_login.return_value = Mock(url="tg://login?token=synthetic", wait=AsyncMock())
                if outcome == "cancelled":
                    f.cancelled = True
                elif outcome == "expired":
                    f.deadline = 0
                with patch("telethon.TelegramClient", return_value=client):
                    login = a.telethon_login(f, root, None, (123, "a" * 32))
                    if outcome == "done":
                        asyncio.run(login)
                        self.assertEqual(json.loads(path.read_text())["session"], "new-session")
                    else:
                        self.assertRaises(ValueError if outcome == "failed" else a.LoginStopped, asyncio.run, login)
                        self.assertEqual(path.read_bytes(), original)
                client.disconnect.assert_awaited_once()
