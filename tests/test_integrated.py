"""Regressions unique to the shared frontend and the receipted TDL clone route."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot_fixture as fixtures
from clone_receipts import Delivered
from relay_delivery import album_batches
from test_monitor_tdl_fallback import photo

app, SOURCE, TARGET = fixtures.app, fixtures.SOURCE, fixtures.TARGET


class IntegratedTests(fixtures.MonitorHarness, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.console_data = Path(self.enterContext(tempfile.TemporaryDirectory()))

    async def test_name_shared_between_bot_and_web_without_changing_routing(self):
        from console_http import Console

        bot = self.bot
        console = Console(bot, app, "demo", bot.console_data)
        bot.task_queue.running = True
        target = bot.target_chat_id  # Marked ID; the same source uses its bare ID.
        await console.command("add-source", {"source": str(TARGET), "name": "同名频道"})
        await console.command(
            "channel-name", {"channel": str(SOURCE), "name": "同名频道"}
        )
        self.assertEqual(bot.channel_label(target), f"同名频道（{target}）")
        self.assertIn(f"同名频道（{SOURCE}）", bot.format_monitor_chat_ids())
        reply = AsyncMock()
        with patch.object(app, "ADMIN_IDS", [61]):
            await bot.run_admin_action(
                SimpleNamespace(sender_id=61),
                ["/channelname", str(target), "主", "归档"],
                reply,
            )
        state = await console.command("state", {})
        self.assertEqual(state["channel_names"][str(TARGET)], "主 归档")
        self.assertEqual(state["channel_names"][str(target)], "主 归档")
        self.assertEqual(state["config"]["target"], str(target))
        await console.command("remove-source", {"source": str(TARGET)})
        self.assertEqual(bot.channel_label(target), f"主 归档（{target}）")
        await console.command("target", {"target": str(SOURCE)})
        self.assertNotIn(TARGET, bot.channel_names)
        cfg = json.loads((bot.console_data / "channels.json").read_text("utf-8"))
        self.assertEqual(
            cfg,
            {"target": SOURCE, "sources": [SOURCE], "names": {str(SOURCE): "同名频道"}},
        )

    async def test_invalid_or_stale_channel_name_cannot_modify_configuration(self):
        from console_http import Console

        bot = self.bot
        console = Console(bot, app, "demo", bot.console_data)
        bot.task_queue.running = True
        payload = {"channel": str(SOURCE), "name": "新的备注"}
        await console.command("channel-name", payload)
        cfg = bot.console_data / "channels.json"
        before = cfg.read_bytes()
        for op, bad in [
            ("channel-name", {**payload, "previous": "旧名"}),
            ("channel-name", {**payload, "channel": "9999"}),
            ("target", {"target": "9999", "name": "过" * 49}),
            ("add-source", {"source": "9999", "name": "两\n行"}),
        ]:
            with self.subTest(op=op, data=bad), self.assertRaises(ValueError):
                await console.command(op, bad)
        self.assertEqual(cfg.read_bytes(), before)
        self.assertEqual(bot.monitor_chat_ids, [SOURCE])
        self.assertNotEqual(bot.target_chat_id, 9999)
        await console.command("channel-name", {**payload, "name": ""})
        self.assertEqual(bot.channel_label(SOURCE), str(SOURCE))

    async def test_channel_inputs_converge_without_network_or_duplicate_monitor(self):
        from console_http import Console
        from demo_transport import DemoTransport

        bot = self.bot
        bot.task_queue.running = True
        bot.demo_transport = DemoTransport(bot.task_queue)
        console = Console(bot, app, "demo", bot.console_data)
        forms = [
            "2345678901",
            "-1002345678901",
            "https://t.me/c/2345678901/",
            "https://t.me/c/2345678901/42",
            "@demo_news",
            "https://t.me/DEMO_NEWS/",
            "https://t.me/demo_news/42",
        ]
        for ref in forms:
            info = await console.command("preview-channel", {"channel": ref})
            self.assertEqual(info["channel"], "2345678901")
        self.assertEqual(info["title"], "演示快讯")
        self.assertEqual(bot.monitor_chat_ids, [SOURCE])
        await console.command(
            "add-source", {"source": "https://t.me/c/2345678901/42", "name": "快讯"}
        )
        with self.assertRaisesRegex(ValueError, "已在监听列表"):
            await console.command("add-source", {"source": "-1002345678901"})
        self.assertEqual(bot.channel_names[2345678901], "快讯")
        await console.command("remove-source", {"source": "-1002345678901"})
        for bad in (
            "-1234567890",
            "https://t.me/+abc123",
            "https://t.me/joinchat/example",
            "https://t.me/c/123/4?comment=8",
            "https://t.me/c/1/2147483648",
            "https://t.me.example.org/c/123/4",
            "@unknown_channel",
        ):
            with self.subTest(ref=bad), self.assertRaises(ValueError):
                await console.command("preview-channel", {"channel": bad})
        self.assertEqual(bot.monitor_chat_ids, [SOURCE])
        bot.client.get_entity.assert_not_awaited()

    async def test_public_bot_confirmation_uses_pinned_id_not_a_second_lookup(self):
        from demo_transport import DemoTransport

        bot = self.bot
        bot.demo_transport = DemoTransport(bot.task_queue)
        reply = AsyncMock()
        event = SimpleNamespace(sender_id=61)
        with patch.object(app, "ADMIN_IDS", [61]):
            await bot.run_admin_action(
                event, ["/monitor", "add", "https://t.me/demo_news/42"], reply
            )
            self.assertEqual(bot.monitor_chat_ids, [SOURCE])
            self.assertIn("演示快讯", reply.await_args.args[0])
            cmd = reply.await_args.args[0].splitlines()[-1].split()
            self.assertEqual(cmd, ["/monitor", "add", "2345678901"])
            await bot.run_admin_action(event, cmd, reply)
            await bot.run_admin_action(
                event, ["/monitor", "set", "-1002345678901,2345678901"], reply
            )
        self.assertEqual(bot.monitor_chat_ids, [2345678901])

    async def test_public_lookup_rejects_nonchannel_and_invite_never_reaches_client(
        self,
    ):
        from telethon import types

        self.bot.client.get_entity.return_value = types.User(id=7)
        with self.assertRaisesRegex(ValueError, "不是广播频道"):
            await self.bot.resolve_channel("https://t.me/some_user/42")
        self.bot.client.get_entity.assert_awaited_once_with("@some_user")
        with self.assertRaisesRegex(ValueError, "不会自动加入"):
            await self.bot.resolve_channel("https://t.me/+example")
        self.assertEqual(self.bot.client.get_entity.await_count, 1)

    def test_range_requires_the_whole_album_and_keeps_distinct_groups(self):
        rows = [
            {"id": i, "type": "message", "raw": {"ID": i, "GroupedID": g}}
            for i, g in [(15, 2), (14, 2), (13, 1), (11, 1), (10, 0)]
        ]

        def plan(lo, hi, is_range=True, single=False):
            return album_batches(
                rows, lo, hi, is_range=is_range, single=single, low=1, high=100
            )

        self.assertEqual(plan(10, 15), [[10], [11, 13], [14, 15]])
        with self.assertRaisesRegex(ValueError, "截断相册"):
            plan(10, 14)
        self.assertEqual(plan(13, 13, False), [[11, 13]])
        self.assertEqual(plan(13, 13, False, True), [[13]])

    async def test_web_retry_returns_original_journal_id_after_restart(self):
        q = self.bot.task_queue

        async def submit(queue, mid=1):
            return await queue.add_task(
                None,
                f"https://t.me/c/{SOURCE}/{mid}",
                None,
                0,
                target_chat_id=TARGET,
                web_key="same-request",
            )

        jid = await submit(q)
        self.assertEqual(await submit(q), jid)
        self.assertEqual(q.queue.qsize(), 1)
        await q.stop()
        restored = self.bot.task_queue = app.TaskQueue(self.bot)
        self.assertEqual(await submit(restored), jid)
        self.assertEqual(restored.queue.qsize(), 1)
        with self.assertRaises(ValueError):
            await submit(restored, 2)

    async def test_monitor_restriction_uses_one_clone_and_edits_with_original_actor(
        self,
    ):
        (self.bot.console_data / "accounts" / "tdl-home" / ".tdl").mkdir(parents=True)
        self.bot.report_monitor_status = AsyncMock()
        self.bot.client.get_messages.side_effect = lambda _, ids: [
            photo(i, 12) for i in (7, 9) if i in ids
        ]
        self.bot.client.forward.side_effect = app.errors.ChatForwardsRestrictedError(
            None
        )
        fake = AsyncMock(return_value=Delivered({7: 80, 9: 81}, 777))
        with (
            patch.object(app, "MONITOR_TDL_FALLBACK", True),
            patch.object(app.relay_delivery, "clone", fake),
        ):
            await self.enqueue(self.event(7, 9, album=12))
            self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
            self.assertEqual(fake.await_args.args[-1], [7, 9])
            m = self.bot.monitor_message_map()
            self.assertEqual(m.sender_role(SOURCE, 7, TARGET), "tdl:777")
            self.assertEqual(m.get(SOURCE, 9, TARGET), 81)
            await self.enqueue(self.event(9, album=12), edited=True)
            self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
            self.assertEqual(fake.await_args.args[-3:], ([9], 81, "777"))
            self.assertEqual(fake.await_count, 2)
            self.assertEqual(self.bot.client.forward.await_count, 1)
