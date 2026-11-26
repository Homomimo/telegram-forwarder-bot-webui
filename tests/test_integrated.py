"""Regressions unique to the shared frontend and the receipted TDL clone route."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telethon import types

import bot_fixture as fixtures
from clone_receipts import Delivered
from launch import prepare_data
from relay_delivery import album_batches
from test_monitor_tdl_fallback import photo

app, SOURCE, TARGET = fixtures.app, fixtures.SOURCE, fixtures.TARGET


class DataDirectoryTests(unittest.TestCase):
    def test_new_install_uses_configured_directory_directly(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp).resolve() / "data"
            with patch.dict("os.environ", {"RELAY_DATA": str(data)}):
                self.assertEqual(prepare_data(), data)
            self.assertTrue(data.is_dir())
            self.assertFalse((data / "live").exists())

    def test_startup_refuses_to_initialize_over_legacy_data(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp).resolve()
            legacy = data / "live"
            legacy.mkdir()
            config = legacy / "channels.json"
            config.write_text('{"target": 1234567890}', encoding="utf-8")
            with patch.dict("os.environ", {"RELAY_DATA": str(data)}):
                with self.assertRaisesRegex(SystemExit, "/data/live"):
                    prepare_data()
            self.assertEqual(list(data.iterdir()), [legacy])
            self.assertEqual(config.read_text(encoding="utf-8"), '{"target": 1234567890}')


class IntegratedTests(fixtures.MonitorHarness, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.console_data = Path(self.enterContext(tempfile.TemporaryDirectory()))

    async def test_channel_pairs_stage_activate_and_keep_manual_default_and_queued_targets(self):
        from console_http import Console

        bot = self.bot
        bot.task_queue.running = True
        secondary, first_target, second_target = SOURCE + 1, TARGET + 1, TARGET + 2
        default = bot.target_chat_id
        bot.update_channels(sources=[SOURCE, secondary])
        console = Console(bot, app, bot.console_data)

        async def change(action, **fields):
            return await console.command('channel-pair', dict(action=action, confirmed=True,
                                                               revision=bot.pairing_revision, **fields))

        config = bot.console_data / 'channels.json'
        legacy = json.loads(config.read_text('utf-8'))
        bot.load_channel_pairs(legacy)
        self.assertFalse(bot.strict_pairs)
        await change('add', source=SOURCE, target=first_target)
        self.assertEqual(bot.automatic_target(SOURCE), TARGET)
        before = config.read_bytes()
        self.assertEqual(bot.pairing_state()['missing'], [str(secondary)])
        for action, fields in [('activate', {}), ('add', {'source': secondary, 'target': first_target}),
                               ('add', {'source': first_target, 'target': second_target})]:
            with self.subTest(action=action, fields=fields), self.assertRaises(ValueError):
                await change(action, **fields)
        self.assertEqual(config.read_bytes(), before)
        await self.enqueue(self.event(10))
        await change('add', source=secondary, target=second_target)
        await change('activate')
        self.assertTrue(bot.strict_pairs)
        self.assertEqual(bot.target_chat_id, default)
        with self.assertRaisesRegex(ValueError, '已改变'):
            await console.command('channel-pair', dict(action='delete', source=SOURCE,
                                                       confirmed=True, revision=0))
        replay = self.event(10)
        replay.message.date = datetime.fromtimestamp(1, timezone.utc)
        await self.enqueue(replay)
        mixed_album = self.event(20, 21, album=42)
        mixed_album.messages[0].date = replay.message.date
        mixed_album.messages[1].date = datetime.fromtimestamp(bot.channel_pairs[SOURCE][2], timezone.utc)
        await self.enqueue(mixed_album)
        await self.enqueue(self.event(11))
        await self.enqueue(self.event(12, channel=secondary))
        await self.enqueue(self.event(13, channel=first_target))
        await change('toggle', source=secondary)
        await self.enqueue(self.event(14, channel=secondary))
        await change('toggle', source=secondary)
        await change('edit', source=SOURCE, target=first_target + 10)
        await self.enqueue(self.event(15))
        with patch.object(app, 'ADMIN_IDS', [61]):
            reply = AsyncMock()
            event = SimpleNamespace(sender_id=61, chat_id=61, respond=AsyncMock(return_value=None))
            await bot.run_admin_action(event, ['/export_range', str(SOURCE), '100', '110'], reply)
        tasks = []
        while not bot.task_queue.queue.empty():
            task = bot.task_queue.queue.get_nowait()
            bot.task_queue.queue.task_done()
            tasks.append((task.mode, task.message_ids[0] if task.message_ids else None, task.target_chat_id))
        self.assertEqual(tasks, [('monitor_copy', 10, TARGET), ('monitor_copy', 11, first_target),
                                 ('monitor_copy', 12, second_target), ('monitor_copy', 15, first_target + 10),
                                 ('export_range', 100, default)])
        fresh = self.make_bot()
        saved = json.loads(config.read_text('utf-8'))
        fresh.load_channel_pairs(saved)
        self.assertTrue(fresh.strict_pairs)
        self.assertEqual(fresh.channel_pairs, bot.channel_pairs)

    async def test_paired_resync_only_uses_matching_target(self):
        bot = self.bot
        target = TARGET + 1
        bot.change_channel_pair('add', SOURCE, target, expected=bot.pairing_revision)
        bot.change_channel_pair('activate', expected=bot.pairing_revision)
        mappings = bot.monitor_message_map()
        mappings.remember(SOURCE, TARGET, {7: 70})
        mappings.remember(SOURCE, target, {8: 80})
        event = SimpleNamespace(sender_id=61, chat_id=61)
        reply = AsyncMock()
        with patch.object(app, 'ADMIN_IDS', [61]):
            await bot.run_admin_action(event, ['/resync', str(SOURCE), '7'], reply)
            self.assertIn('没有转发映射', reply.await_args.args[0])
            self.assertTrue(bot.task_queue.queue.empty())
            await bot.run_admin_action(event, ['/resync', str(SOURCE), '8'], reply)
        task = bot.task_queue.queue.get_nowait()
        bot.task_queue.queue.task_done()
        self.assertEqual((task.mode, task.target_chat_id, task.message_ids), ('monitor_edit', target, [8]))

    async def test_disable_pairs_persists_legacy_route_and_retains_pairs_mappings_and_queued_targets(self):
        from console_http import Console

        bot = self.bot
        bot.task_queue.running = True
        pair_target = TARGET + 1
        bot.change_channel_pair('add', SOURCE, pair_target, expected=bot.pairing_revision)
        bot.change_channel_pair('activate', expected=bot.pairing_revision)
        saved_pairs = bot.channel_pairs.copy()
        mappings = bot.monitor_message_map()
        mappings.remember(SOURCE, pair_target, {1: 101})
        mappings.remember(SOURCE, TARGET, {2: 102})
        await self.enqueue(self.event(3))
        console = Console(bot, app, bot.console_data)
        payload = {'action': 'deactivate', 'revision': bot.pairing_revision}
        with self.assertRaisesRegex(ValueError, '请确认'):
            await console.command('channel-pair', payload)
        self.assertTrue(bot.strict_pairs)
        await console.command('channel-pair', {**payload, 'confirmed': True})
        self.assertFalse(bot.strict_pairs)
        self.assertEqual(bot.channel_pairs, saved_pairs)
        self.assertEqual(mappings.get(SOURCE, 1, pair_target), 101)
        self.assertEqual(mappings.get(SOURCE, 2, TARGET), 102)
        replay = self.event(1)
        replay.message.date = datetime.fromtimestamp(bot.legacy_resumed_at - 1, timezone.utc)
        await self.enqueue(replay)
        await self.enqueue(self.event(4))
        expected = [(3, pair_target), (4, TARGET)]
        self.assertEqual(bot.task_queue.queue.qsize(), len(expected))
        for mid, target in expected:
            task = bot.task_queue.queue.get_nowait()
            bot.task_queue.queue.task_done()
            self.assertEqual((task.message_ids, task.target_chat_id), ([mid], target))
        fresh = self.make_bot()
        config = json.loads((bot.console_data / 'channels.json').read_text('utf-8'))
        fresh.load_channel_pairs(config)
        self.assertFalse(fresh.strict_pairs)
        self.assertEqual(fresh.channel_pairs, saved_pairs)
        self.assertEqual(fresh.legacy_resumed_at, bot.legacy_resumed_at)
        fresh.change_channel_pair('activate', expected=fresh.pairing_revision)
        self.assertEqual(fresh.automatic_target(SOURCE), pair_target)

    async def test_web_pair_persists_recognized_titles_and_preserves_custom_remark(self):
        from console_http import Console

        bot = self.bot
        bot.task_queue.running = True
        console = Console(bot, app, bot.console_data)
        destination = TARGET + 1
        bot.update_channels(names={SOURCE: '已有备注'})
        with patch.object(bot, 'resolve_channel', AsyncMock(side_effect=[
            (SOURCE, '真实源频道'), (destination, '目标频道\n归档'),
        ])):
            source = await console.command('preview-channel', {'channel': '@source'})
            target = await console.command('preview-channel', {'channel': '@destination'})
        await console.command('channel-pair', dict(action='add', confirmed=True,
                                                   revision=bot.pairing_revision, source=source['channel'],
                                                   target=target['channel'], source_title=source['title'],
                                                   target_title=target['title']))
        saved = json.loads((bot.console_data / 'channels.json').read_text('utf-8'))
        self.assertEqual(saved['names'], {str(SOURCE): '已有备注', str(destination): '目标频道 归档'})
        self.assertEqual((await console.command('state', {}))['channel_names'][str(destination)], '目标频道 归档')
        await console.command('channel-pair', dict(action='edit', confirmed=True,
                                                   revision=bot.pairing_revision, source=str(SOURCE),
                                                   target=str(destination)))
        self.assertEqual(bot.channel_names[destination], '目标频道 归档')

    async def test_bot_link_http_redemption_and_qr_requests_require_valid_session(self):
        from console_http import Console, Handler
        from web_sessions import COOKIE, LINK_TTL, WebSessions

        clock = Mock(return_value=1)
        sessions = WebSessions(clock)
        handler = object.__new__(Handler)
        handler.allowed = Mock(return_value=True)
        handler.reply = Mock()
        handler.server = SimpleNamespace(console=Console(self.bot, app, self.bot.console_data))
        self.bot.tdl.tdl_path = '/test/tdl'

        def post(path, payload, *, token=None, csrf=None):
            handler.path = path
            body = json.dumps(payload).encode()
            handler.rfile = BytesIO(body)
            handler.headers = Message()
            handler.headers['Host'] = '203.0.113.10:8765'
            handler.headers['Content-Type'] = 'application/json'
            handler.headers['Content-Length'] = str(len(body))
            handler.headers['X-Relay-Request'] = '1'
            if token:
                handler.headers['Cookie'] = f'{COOKIE}={token}'
            if csrf:
                handler.headers['X-CSRF-Token'] = csrf
            handler.do_POST()
            return handler.reply.call_args

        with patch('console_http.SESSIONS', sessions), patch.object(app, 'ADMIN_IDS', [61]):
            self.assertEqual(post('/api/auth/login', {'username': 'admin', 'password': 'old'}).args[0], 401)
            old = sessions.issue_link(61)
            code = sessions.issue_link(61)
            self.assertEqual(post('/api/auth/bot-link', {'code': old}).args[0], 401)
            self.assertEqual(post('/api/auth/bot-link', {'code': sessions.issue_link(72)}).args[0], 401)
            redeemed = post('/api/auth/bot-link', {'code': code})
            self.assertEqual(redeemed.args[0], 200)
            cookie = redeemed.kwargs['cookie']
            self.assertIn('HttpOnly; SameSite=Strict; Secure', cookie)
            token = cookie.split('=', 1)[1].split(';')[0]
            csrf = redeemed.args[1]['csrf']
            self.assertEqual(post('/api/auth/bot-link', {'code': code}).args[0], 401)
            for account in ('tdl', 'telethon'):
                with patch('console_http.accounts.manager.command', return_value={'ok': True}) as qr:
                    self.assertEqual(post('/api/login/start', {'provider': account}).args[0], 401)
                    self.assertEqual(post('/api/login/start', {'provider': account}, token=token).args[0], 403)
                    self.assertEqual(post('/api/login/start', {'provider': account}, token=token, csrf=csrf).args[0], 200)
                    qr.assert_called_once_with('start', {'provider': account}, self.bot.console_data, '/test/tdl')
            expiring = sessions.issue_link(61)
            clock.return_value += LINK_TTL
            self.assertEqual(post('/api/auth/bot-link', {'code': expiring}).args[0], 401)
            self.assertEqual(post('/api/auth/logout', {}, token=token, csrf=csrf).args[0], 200)
            self.assertEqual(post('/api/login/start', {'kind': 'tdl'}, token=token, csrf=csrf).args[0], 401)

    async def test_web_mappings_list_only_confirmed_pairs_and_requires_session(self):
        from console_http import Console, Handler

        bot = self.bot
        console = Console(bot, app, bot.console_data)
        mappings = bot.monitor_message_map()
        mappings.remember(SOURCE, TARGET, {mid: 100 + mid for mid in range(1, 12)})
        mappings.reserve(SOURCE, TARGET, [12])
        page = await console.command('mappings', {'page': '1'})
        self.assertEqual((page['total'], page['pages'], page['page']), (11, 2, 1))
        self.assertEqual([row['source_message'] for row in page['rows']], list(range(11, 1, -1)))
        self.assertEqual(page['rows'][0]['target_url'], f'https://t.me/c/{TARGET}/111')
        last = await console.command('mappings', {'page': '99'})
        self.assertEqual(last['page'], 2)
        self.assertEqual([row['source_message'] for row in last['rows']], [1])
        with self.assertRaisesRegex(ValueError, '页码必须是正整数'):
            await console.command('mappings', {'page': '0'})

        handler = object.__new__(Handler)
        handler.path = '/api/mappings?page=2'
        handler.allowed = Mock(return_value=True)
        handler.session = Mock(return_value=None)
        handler.reply = Mock()
        handler.server = SimpleNamespace(console=SimpleNamespace(dispatch=Mock(return_value=last)))
        handler.do_GET()
        self.assertEqual(handler.reply.call_args.args[0], 401)
        handler.server.console.dispatch.assert_not_called()
        handler.session.return_value = 'authenticated'
        handler.do_GET()
        handler.server.console.dispatch.assert_called_once_with('mappings', {'page': '2'})
        handler.reply.assert_called_with(200, last)

    async def test_web_clear_mappings_requires_confirmation_and_preserves_copy_state(self):
        from console_http import Console, Handler

        bot = self.bot
        bot.task_queue.running = True
        console = Console(bot, app, bot.console_data)
        mappings = bot.monitor_message_map()
        mappings.remember(SOURCE, TARGET, {1: 101})
        mappings.reserve(SOURCE, TARGET, [2])
        with self.assertRaisesRegex(ValueError, '请确认'):
            await console.command('clear-mappings', {})
        with patch.object(bot.task_queue, 'current_task', SimpleNamespace(mode='monitor_edit')):
            with self.assertRaisesRegex(ValueError, '正在同步'):
                await console.command('clear-mappings', {'confirmed': True})
        self.assertEqual(mappings.get(SOURCE, 1, TARGET), 101)
        result = await console.command('clear-mappings', {'confirmed': True})
        self.assertEqual(result, {'removed': 1})
        self.assertEqual(mappings.list_mappings(1, 10)['rows'], [])
        self.assertEqual(mappings.copy_state(SOURCE, 1, TARGET), app.TaskStatus.COMPLETED)
        self.assertFalse(mappings.can_await_mapping(SOURCE, 1, TARGET))
        self.assertEqual(mappings.copy_state(SOURCE, 2, TARGET), app.TaskStatus.UNCONFIRMED)
        self.assertEqual(await console.command('clear-mappings', {'confirmed': True}), {'removed': 0})

        handler = object.__new__(Handler)
        handler.path = '/api/clear-mappings'
        handler.allowed = Mock(return_value=True)
        handler.session = Mock(return_value='valid')
        handler.reply = Mock()
        handler.server = SimpleNamespace(console=SimpleNamespace(dispatch=Mock(return_value=result)))
        body = b'{"confirmed":true}'
        handler.headers = Message()
        for key, value in {'Content-Type': 'application/json', 'Content-Length': str(len(body)),
                           'X-Relay-Request': '1', 'X-CSRF-Token': 'wrong'}.items():
            handler.headers[key] = value
        handler.rfile = BytesIO(body)
        handler.do_POST()
        self.assertEqual(handler.reply.call_args.args[0], 403)
        handler.server.console.dispatch.assert_not_called()
        handler.headers.replace_header('X-CSRF-Token', 'valid')
        handler.rfile.seek(0)
        handler.do_POST()
        handler.server.console.dispatch.assert_called_once_with('clear-mappings', {'confirmed': True})
        handler.reply.assert_called_with(200, result)

    async def test_name_shared_between_bot_and_web_without_changing_routing(self):
        from console_http import Console

        bot = self.bot
        console = Console(bot, app, bot.console_data)
        bot.task_queue.running = True
        target = bot.target_chat_id  # Marked ID; the same source uses its bare ID.
        await console.command("add-source", {"source": str(TARGET), "name": "同名频道"})
        await console.command(
            "channel-name", {"channel": str(SOURCE), "name": "同名频道"}
        )
        self.assertEqual(bot.channel_label(target), f"同名频道（{TARGET}）")
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
        self.assertEqual(bot.channel_label(target), f"主 归档（{TARGET}）")
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
        console = Console(bot, app, bot.console_data)
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

    async def test_web_queue_reviews_and_history_share_channel_labels(self):
        from console_http import Console

        bot = self.bot
        bot.update_channels(names={SOURCE: '来源', TARGET: '归档'})
        target = bot.target_chat_id
        private = f'https://t.me/c/{SOURCE}/42'
        public = 'https://t.me/example_source/43'
        queue = bot.task_queue
        await queue.add_task(None, private, None, 61, target_chat_id=target)
        await queue.add_task(None, public, None, 61, target_chat_id=target)
        review = app.ForwardTask(None, public, None, 61, target, source_chat_id=SOURCE)
        queue.journal.save(review, 'review')
        bot.forward_log.get_recent.return_value = [
            dict(id='1', to=target, **{'from': private}),
            dict(id='2', to=TARGET + 1, **{'from': public}),
            dict(id='3', to=target, source_channel=SOURCE, **{'from': public}),
        ]
        state = await Console(bot, app, bot.console_data).command('state', {})
        labels = (f'来源（{SOURCE}）', f'归档（{TARGET}）')
        for item in (state['queue']['queued'][0], state['reviews'][0], state['history'][0], state['history'][2]):
            self.assertEqual((item['source_label'], item['target_label']), labels)
        self.assertEqual(state['queue']['queued'][1]['source_label'], '@example_source')
        self.assertEqual(state['history'][1]['target_label'], str(TARGET + 1))

    async def test_web_history_exposes_validated_target_post_urls(self):
        from console_http import Console

        bot = self.bot
        base = f'https://t.me/c/{TARGET}'
        common = {'to': bot.target_chat_id, 'from': f'https://t.me/c/{SOURCE}/1'}
        bot.forward_log.get_recent.return_value = [
            dict(common, id='album', members=[[1, 101], [2, 0], [3, -1]], album=True, success=False),
            dict(common, id='legacy', target_message_id=102, success=True),
            dict(common, id='uncertain', target_message_id=103, success=False),
            dict(common, id='invalid', to=-123, target_message_id=104, success=True),
        ]
        state = await Console(bot, app, bot.console_data).command('state', {})
        history = {row['id']: row for row in state['history']}
        self.assertEqual(history['album']['target_post_base'], base)
        self.assertTrue(history['album']['album'])
        self.assertEqual(history['album']['members'], [[1, 101], [2, 0], [3, -1]])
        self.assertEqual(history['legacy']['legacy_target_url'], f'{base}/102')
        self.assertIsNone(history['uncertain']['legacy_target_url'])
        self.assertIsNone(history['invalid']['target_post_base'])
        self.assertIsNone(history['invalid']['legacy_target_url'])

    async def test_channel_inputs_converge_without_network_or_duplicate_monitor(self):
        from console_http import Console

        bot = self.bot
        bot.task_queue.running = True
        bot.client.get_entity.return_value = types.Channel(
            2345678901, "测试频道", types.ChatPhotoEmpty(), None, broadcast=True
        )
        console = Console(bot, app, bot.console_data)
        forms = [
            "2345678901",
            "-1002345678901",
            "https://t.me/c/2345678901/",
            "https://t.me/c/2345678901/42",
            "@test_news",
            "https://t.me/TEST_NEWS/",
            "https://t.me/test_news/42",
        ]
        for ref in forms:
            info = await console.command("preview-channel", {"channel": ref})
            self.assertEqual(info["channel"], "2345678901")
        self.assertEqual(info["title"], "测试频道")
        self.assertEqual(bot.monitor_chat_ids, [SOURCE])
        await console.command(
            "add-source", {"source": "https://t.me/c/2345678901/42", "name": "快讯"}
        )
        with self.assertRaisesRegex(ValueError, "已在监听列表"):
            await console.command("add-source", {"source": "-1002345678901"})
        self.assertEqual(bot.channel_names[2345678901], "快讯")
        await console.command("remove-source", {"source": "-1002345678901"})
        bot.client.get_entity.reset_mock()
        bot.client.get_entity.side_effect = ValueError("unknown channel")
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
        bot.client.get_entity.assert_awaited_once_with("@unknown_channel")

    async def test_public_bot_confirmation_uses_pinned_id_not_a_second_lookup(self):
        bot = self.bot
        bot.client.get_entity.return_value = types.Channel(
            2345678901, "测试频道", types.ChatPhotoEmpty(), None, broadcast=True
        )
        reply = AsyncMock()
        event = SimpleNamespace(sender_id=61)
        with patch.object(app, "ADMIN_IDS", [61]):
            await bot.run_admin_action(
                event, ["/monitor", "add", "https://t.me/test_news/42"], reply
            )
            self.assertEqual(bot.monitor_chat_ids, [SOURCE])
            self.assertIn("测试频道", reply.await_args.args[0])
            cmd = reply.await_args.args[0].splitlines()[-1].split()
            self.assertEqual(cmd, ["/monitor", "add", "2345678901"])
            await bot.run_admin_action(event, cmd, reply)
            await bot.run_admin_action(
                event, ["/monitor", "set", "-1002345678901,2345678901"], reply
            )
        self.assertEqual(bot.monitor_chat_ids, [2345678901])
        bot.client.get_entity.assert_awaited_once_with("@test_news")

    async def test_public_lookup_rejects_nonchannel_and_invite_never_reaches_client(
        self,
    ):
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
