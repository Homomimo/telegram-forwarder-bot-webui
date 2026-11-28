"""Drive the registered bot handlers with real Telethon keyboard objects, without a Telegram session."""
import asyncio
import itertools
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from telethon import TelegramClient, events, types
from bot_fixture import app, FakeClient, SOURCE, TARGET
from web_sessions import WebSessions
from file_staging import FileStaging
from test_monitor_tdl_fallback import photo

UID = 61


class PrivatePanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(patch.stopall)
        for key, val in dict(ADMIN_IDS=[UID], SUBMITTER_IDS={71}, USER_MONITOR_ENABLED=False,
                             MONITOR_MAP_RETENTION_DAYS=0, MONITOR_MAP_FILE=str(Path(tmp.name)/'map.db'),
                             TelegramClient=FakeClient, ForwardLog=Mock).items():
            patch.object(app, key, val).start()
        self.bot = app.TelegramBot()
        self.bot.monitor_chat_ids, self.bot.target_chat_id = [SOURCE], TARGET
        self.client = self.bot.client
        self.message_ids = itertools.count(1)
        self.messages = {}
        self.client.send_message.side_effect = self.send
        self.client.edit_message.side_effect = self.edit
        self.client.delete_messages = AsyncMock(side_effect=self.delete_messages)
        self.bot.task_queue.start = AsyncMock()  # keep admitted tasks available for inspection
        await self.bot.start()
        self.new, self.cb = (self.client.handlers[k] for k in (events.NewMessage, events.CallbackQuery))
        self.bot.do_forward = AsyncMock()
        self.addAsyncCleanup(self.bot.stop)

    def send(self, chat, text, **kw):
        mid = next(self.message_ids)
        msg = NS(id=mid, chat_id=chat, raw_text=text, out=True, buttons=kw.get('buttons'),
                 client=self.client, edit=AsyncMock(), delete=AsyncMock())
        self.messages[mid] = msg
        return msg

    def edit(self, chat, mid, text, **kw):
        msg = self.messages[mid]
        self.assertEqual(msg.chat_id, chat)
        msg.raw_text, msg.buttons = text, kw.get('buttons')
        return msg

    def delete_messages(self, chat, mids):
        for mid in mids:
            self.assertEqual(self.messages[mid].chat_id, chat)
            del self.messages[mid]

    @property
    def session(self):
        return self.bot.admin_panel.sessions[UID]

    @property
    def page(self):
        return self.messages[self.session.mid]

    def button(self, label):
        current = self.messages[self.session.mid or self.session.sync_controls_mid]
        markup = TelegramClient.build_reply_markup(current.buttons)
        return next(b.type.data for row in markup.rows for b in row.buttons if b.text == label)

    async def hit(self, data, uid=UID, chat=UID, mid=None, via_inline=False, message=None):
        ev = NS(data=data, sender_id=uid, chat_id=chat,
                message_id=(self.session.mid or self.session.sync_controls_mid) if mid is None else mid,
                via_inline=via_inline, answer=AsyncMock(), respond=AsyncMock(side_effect=lambda t, **kw: self.send(chat,t,**kw)),
                client=self.client, get_message=AsyncMock(return_value=message))
        await self.cb(ev)
        return ev

    async def click(self, label):
        ev = await self.hit(self.button(label))
        ev.answer.assert_awaited_once()
        self.assertFalse(ev.answer.await_args.kwargs.get('alert'))
        return ev

    async def text(self, value, *, reply=None, uid=UID, chat=UID, forwarded=False):
        msg = NS(id=next(self.message_ids), text=value, raw_text=value, forward=forwarded, grouped_id=None,
                 post=False, peer_id=types.PeerUser(uid) if chat > 0 else types.PeerChat(-chat),
                 reply_to_msg_id=reply, media=None)
        ev = NS(message=msg, sender_id=uid, chat_id=chat, out=False, is_channel=False,
                raw_text=value, client=self.client, get_reply_message=AsyncMock(return_value=self.messages.get(reply)),
                respond=AsyncMock(side_effect=lambda t, **kw: self.send(chat,t,**kw)))
        await self.new(ev)
        return ev

    async def reply(self, text):
        return await self.text(text, reply=self.session.prompt_id)

    async def test_sync_replaces_home_with_controls_and_force_reply_and_cancels_both(self):
        home = self.page
        await self.click('🔄 手动同步')
        prompt = self.session.prompt_id
        controls = self.session.sync_controls_mid
        self.assertEqual(list(self.messages), [controls, prompt])
        self.client.delete_messages.assert_awaited_once_with(UID, [home.id])
        self.client.edit_message.assert_not_awaited()
        key = self.button('⬅️ 取消并返回')
        await self.click('⬅️ 取消并返回')
        self.assertEqual(self.session.prompt_id, 0)
        self.assertNotIn(prompt, self.messages)
        self.assertNotIn(controls, self.messages)
        self.assertEqual(self.page.raw_text, self.bot.admin_panel.home_view()[0])
        duplicate = await self.hit(key, mid=controls)
        self.assertTrue(duplicate.answer.await_args.kwargs['alert'])
        for mid in (prompt, controls):
            old = await self.text(f'https://t.me/c/{SOURCE}/42', reply=mid)
            self.assertIn('已失效', old.respond.await_args.args[0])
        self.bot.do_forward.assert_not_awaited()
        self.assertTrue(self.bot.task_queue.queue.empty())

    async def test_sync_prompt_can_return_to_home_without_a_text_reply(self):
        self.bot.channel_names[TARGET] = '归档频道'
        await self.click('🔄 手动同步')
        prompt = self.session.prompt_id
        controls = self.messages[self.session.sync_controls_mid]
        self.assertIn('手动同步', controls.raw_text)
        self.assertIn(f'当前目标：归档频道（{TARGET}）', controls.raw_text)
        self.assertNotIn('回复「取消」', controls.raw_text)
        self.assertIsInstance(self.messages[prompt].buttons, types.ReplyKeyboardForceReply)
        markup = TelegramClient.build_reply_markup(controls.buttons)
        self.assertEqual([[button.text for button in row.buttons] for row in markup.rows],
                         [['⬅️ 取消并返回']])
        await self.click('⬅️ 取消并返回')
        self.assertEqual(list(self.messages), [self.page.id])
        self.assertEqual(self.page.raw_text, self.bot.admin_panel.home_view()[0])
        self.assertTrue(self.bot.task_queue.queue.empty())

    async def test_sync_force_reply_send_failure_removes_staged_controls(self):
        home = self.page

        def send(chat, text, **kwargs):
            if isinstance(kwargs.get('buttons'), types.ReplyKeyboardForceReply):
                raise ConnectionError('reply prompt unavailable')
            return self.send(chat, text, **kwargs)

        with patch.object(self.client, 'send_message', side_effect=send), self.assertLogs('admin_panel', 'ERROR'):
            await self.click('🔄 手动同步')
        self.assertIs(self.page, home)
        self.assertEqual([message.id for message in self.messages.values() if message.buttons], [home.id])
        await self.click('🔄 手动同步')
        self.assertIn(self.session.prompt_id, self.messages)

    async def test_replayed_cancel_after_restart_never_reports_missing_prompt(self):
        await self.click('🔄 手动同步')
        prompt = self.session.prompt_id
        cancel = await self.reply('取消')
        self.assert_input_receipt(cancel, '已取消')
        self.assertNotIn(prompt, self.messages)
        await self.text('/start')
        self.bot.admin_panel.sessions.clear()
        self.bot.task_queue.journal.close()
        self.bot._bot_message_ids.clear()
        cancel.respond.reset_mock()
        cancel.get_reply_message.return_value = None
        await self.new(cancel)
        cancel.respond.assert_not_awaited()
        cancel.get_reply_message.assert_not_awaited()
        (await self.text('取消', reply=prompt)).respond.assert_not_awaited()
        self.bot.do_forward.assert_not_awaited()

    async def test_start_reopens_home_when_telegram_is_still_replying_to_prompt(self):
        await self.click('🔄 手动同步')
        prompt = self.session.prompt_id
        opened = await self.text('/start', reply=prompt)
        opened.respond.assert_awaited_once()
        self.assertEqual(self.page.raw_text, self.bot.admin_panel.home_view()[0])
        self.assertEqual(list(self.messages), [self.page.id])
        self.assertFalse(self.session.prompt_id)

    async def test_clicking_legacy_panel_cleans_only_its_verified_message(self):
        current = self.page
        key = self.button('🎯 目标频道')
        legacy = self.send(UID, '旧面板', buttons=current.buttons)
        legacy.reply_markup = TelegramClient.build_reply_markup(legacy.buttons)
        failed = await self.hit(key + b'forged', mid=legacy.id, message=legacy)
        self.assertTrue(failed.answer.await_args.kwargs['alert'])
        self.assertIn(legacy.id, self.messages)
        await self.hit(key, mid=legacy.id, message=legacy)
        self.assertEqual(list(self.messages), [current.id])
        await self.text('/start')
        self.assertIs(self.page, current)

    async def test_prompt_failure_preserves_old_panel(self):
        root = self.page
        with patch.object(self.client, 'send_message', side_effect=ConnectionError('prompt unavailable')):
            with self.assertLogs('admin_panel', 'ERROR'):
                await self.click('🔄 手动同步')
        self.client.delete_messages.assert_not_awaited()
        self.assertIn(root.id, self.messages)
        await self.click('🔄 手动同步')
        self.assertEqual(list(self.messages), [self.session.sync_controls_mid, self.session.prompt_id])

    async def test_delete_failure_disables_previous_panel_buttons(self):
        replacement = self.page
        with patch.object(self.client, 'delete_messages', side_effect=ConnectionError('delete unavailable')):
            with self.assertLogs('admin_panel', 'WARNING'):
                await self.click('🔄 手动同步')
        self.assertIsNone(replacement.buttons)
        self.assertEqual(replacement.raw_text, '请使用最新的机器人消息。')
        self.assert_input_receipt(await self.reply('取消'), '已取消')

    async def test_replacement_is_durable_before_the_previous_panel_is_deleted(self):
        home = self.page
        panel = self.bot.admin_panel

        def delete(chat, mids):
            current = panel.journal.reusable_panel(panel.bot_id, UID)
            self.assertEqual(current, self.session.mid)
            self.assertNotIn(current, mids)
            self.assertIn(current, self.messages)
            self.delete_messages(chat, mids)

        with patch.object(self.client, 'delete_messages', side_effect=delete):
            await self.click('🎯 目标频道')
        self.assertNotIn(home.id, self.messages)
        self.assertEqual(list(self.messages), [self.page.id])

    async def test_failed_persistence_retracts_the_new_prompt_and_keeps_old_buttons(self):
        home = self.page
        with patch.object(self.bot.admin_panel.journal, 'activate_panel', side_effect=OSError('disk full')):
            with self.assertLogs('admin_panel', 'ERROR'):
                await self.click('🔄 手动同步')
        self.assertIs(self.page, home)
        self.assertEqual([m.id for m in self.messages.values() if m.buttons], [home.id])
        await self.click('🎯 目标频道')
        self.assertNotIn(home.id, self.messages)

    async def test_flog_pages_keep_position_as_records_arrive_expire_or_clear(self):
        self.bot.channel_names.update({SOURCE: '素材<库>', TARGET: '归档频道'})
        rows = [dict(id=str(i), date='date', to=TARGET, success=True,
                     **{'from': f'https://t.me/c/{SOURCE}/{i}'}) for i in range(1, 13)]
        self.bot.forward_log.get_all.side_effect = lambda: list(rows)
        await self.text('/flog 2')
        self.assertIn('#7　', self.page.raw_text)
        self.assertIn(f'源频道：素材&lt;库&gt;（{SOURCE}）', self.page.raw_text)
        rows.append(dict(rows[-1], id='13'))
        await self.click('#7 详情'); self.assertIn('#7　', self.page.raw_text)
        self.assertIn(f'目标频道：归档频道（{TARGET}）', self.page.raw_text)
        await self.click('⬅️ 返回记录'); self.assertIn('第 2/3 页', self.page.raw_text)
        self.assertNotIn('#8　', self.page.raw_text)
        await self.click('上一页'); self.assertNotIn('#13　', self.page.raw_text)
        await self.click('🔄 刷新'); self.assertIn('#13　', self.page.raw_text)
        await self.text('/flog id 1'); self.assertIn('#1　', self.page.raw_text)
        await self.click('⬅️ 返回记录'); self.assertIn('第 3/3 页', self.page.raw_text)
        rows.pop(0)
        await self.click('#1 详情'); self.assertIn('已过期', self.page.raw_text)
        rows.clear()
        await self.click('⬅️ 返回记录'); self.assertIn('暂无转发记录', self.page.raw_text)

    async def test_mappings_menu_and_command_are_private_and_paginated(self):
        self.bot.channel_names[SOURCE] = '来源 <频道>'
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {mid: 100 + mid for mid in range(1, 12)},
                                                album_member=True)
        await self.click('🔗 源帖映射')
        self.assertTrue(self.page.raw_text.startswith('自动复制的源帖映射\n'))
        self.assertIn('查看已确认自动复制的源帖及其对应的目标帖。点击链接可打开对应消息。', self.page.raw_text)
        self.assertIn('第 1/2 页 · 共 11 条', self.page.raw_text)
        self.assertIn('来源 &lt;频道&gt;', self.page.raw_text)
        self.assertIn(f'href="https://t.me/c/{TARGET}/111?single"', self.page.raw_text)
        await self.click('下一页')
        self.assertIn('第 2/2 页', self.page.raw_text)
        self.assertIn(f'href="https://t.me/c/{SOURCE}/1?single"', self.page.raw_text)
        await self.text('/mappings 1')
        self.assertIn('第 1/2 页', self.page.raw_text)
        group = await self.text('/mappings', chat=-456)
        self.assertIn('私聊', group.respond.await_args.args[0])
        self.assertNotIn('https://', group.respond.await_args.args[0])

    async def test_mappings_clear_requires_panel_confirmation_and_retains_deduplication(self):
        mappings = self.bot.monitor_message_map()
        mappings.remember(SOURCE, TARGET, {42: 101})
        await self.click('🔗 源帖映射')
        await self.click('🗑 清理全部映射…')
        await self.click('取消')
        self.assertEqual(mappings.get(SOURCE, 42, TARGET), 101)
        await self.click('🔗 源帖映射')
        await self.click('🗑 清理全部映射…')
        await self.click('确认执行')
        self.assertIn('已清理 1 条源帖映射', self.page.raw_text)
        self.assertEqual(mappings.copy_state(SOURCE, 42, TARGET), app.TaskStatus.COMPLETED)
        await self.text('/mappings')
        self.assertIn('暂无已确认的源帖映射', self.page.raw_text)
        self.assertNotIn('ask:mapclear', self.session.choices.values())

    async def test_bot_can_add_activate_and_pause_channel_pair(self):
        await self.click('📡 监听频道')
        await self.click('🔀 频道配对')
        self.assertIn('自动复制仍使用默认目标频道', self.page.raw_text)
        self.assertNotIn('pair:activate', self.session.choices.values())
        await self.click('➕ 新增配对')
        destination = TARGET + 1
        await self.reply(f'{SOURCE}，{destination}')
        self.assertIn(f'{SOURCE} → {destination}', self.page.raw_text)
        self.assertFalse(self.bot.channel_pairs)
        await self.click('确认执行')
        self.assertEqual(self.bot.automatic_target(SOURCE), TARGET)
        await self.click('⬅️ 返回面板')
        await self.click('📡 监听频道')
        await self.click('🔀 频道配对')
        await self.click('启用严格一对一')
        await self.click('确认执行')
        self.assertEqual(self.bot.automatic_target(SOURCE), destination)
        await self.click('⬅️ 返回面板')
        await self.click('📡 监听频道')
        self.assertNotIn('input:add', self.session.choices.values())
        await self.click('🔀 频道配对')
        await self.click(f'{SOURCE} → {destination} · 启用')
        await self.click('暂停…')
        await self.click('确认执行')
        self.assertIsNone(self.bot.automatic_target(SOURCE))
        self.assertEqual(self.bot.target_chat_id, TARGET)

    async def test_bot_saves_recognized_pair_names_after_confirmation(self):
        await self.click('📡 监听频道')
        await self.click('🔀 频道配对')
        await self.click('➕ 新增配对')
        destination = TARGET + 1
        resolve = AsyncMock(side_effect=[(SOURCE, '科技快讯'), (destination, '快讯归档')])
        with patch.object(self.bot, 'resolve_channel', resolve):
            await self.reply('@news,@archive')
        self.assertIn('科技快讯', self.page.raw_text)
        self.assertNotIn(SOURCE, self.bot.channel_names)
        await self.click('确认执行')
        await self.click('⬅️ 返回面板')
        await self.click('📡 监听频道')
        await self.click('🔀 频道配对')
        self.assertTrue(self.button(f'科技快讯（{SOURCE}） → 快讯归档（{destination}） · 启用'))

    async def test_bot_confirms_disabling_strict_pairs_without_legacy_settings(self):
        bot = self.bot
        bot.target_chat_id = None
        bot.monitor_chat_ids = []
        bot.change_channel_pair('add', SOURCE, TARGET, expected=bot.pairing_revision)
        bot.change_channel_pair('activate', expected=bot.pairing_revision)
        await self.click('📡 监听频道')
        await self.click('🔀 频道配对')
        await self.click('关闭严格一对一…')
        self.assertIn('旧版监听源：未设置', self.page.raw_text)
        self.assertIn('默认目标频道：未设置', self.page.raw_text)
        self.assertIn('关闭后不会自动转发', self.page.raw_text)
        await self.click('取消')
        self.assertTrue(bot.strict_pairs)
        await self.click('📡 监听频道')
        await self.click('🔀 频道配对')
        await self.click('关闭严格一对一…')
        await self.click('确认执行')
        self.assertFalse(bot.strict_pairs)
        self.assertEqual(bot.channel_pairs[SOURCE][:2], (TARGET, True))

    async def test_webui_url_only_appears_beside_guide_in_configured_private_home(self):
        label = '🌐 打开 WebUI'
        panel = self.bot.admin_panel
        self.assertFalse(any(b.text == label for row in self.page.buttons for b in row))
        self.assertNotIn('扫码登录', self.page.raw_text)
        await self.click('⚙️ 系统管理')
        self.assertFalse(any(b.text == label for row in self.page.buttons for b in row))
        await self.click('⬅️ 返回面板')

        panel.webui_url = 'https://203.0.113.10:9443'
        await self.text('/start')
        markup = TelegramClient.build_reply_markup(self.page.buttons)
        home_link, guide_button = markup.rows[0].buttons
        self.assertEqual(home_link.text, label)
        self.assertIsInstance(home_link.type, types.InlineButtonTypeCallback)
        self.assertEqual(guide_button.text, '📖 使用向导')
        self.assertEqual([[b.text for b in row.buttons] for row in markup.rows], [
            [label, '📖 使用向导'],
            ['📥 下载暂存'],
            ['🎯 目标频道', '📡 监听频道'],
            ['🔄 手动同步', '📦 批量转发'],
            ['📋 任务队列', '📝 转发记录'],
            ['🔗 源帖映射', '⚙️ 系统管理'],
        ])
        self.assertTrue(self.page.raw_text.endswith(
            '🌐 首次使用：点击「🌐 打开 WebUI」获取一次性登录链接，打开后进入「工作台设置」，'
            '按需扫码登录 TDL 转发账号或 Telethon 用户会话。\n\n'
            '命令用法和扫码说明见「📖 使用向导」。'
        ))
        home = self.page.raw_text
        self.assertEqual(home.count('\n• '), 3)
        await self.click('📖 使用向导')
        guide = self.page.raw_text
        self.assertEqual(guide, app.HELP_MESSAGE_ADMIN)
        for cmd in ('/showto', '/forwardto', '/channelname', '/monitor', '/resync',
                    '/export_range', '/queue', '/tdlstop', '/flog', '/mappings', '/restart'):
            self.assertNotIn(cmd, home)
            self.assertIn(cmd, guide)
        await self.click('⬅️ 返回面板')
        await self.click('🔗 源帖映射')
        self.assertIn('暂无已确认的源帖映射。', self.page.raw_text)
        await self.click('⬅️ 返回面板')
        await self.click('⚙️ 系统管理')
        markup = TelegramClient.build_reply_markup(self.page.buttons)
        self.assertFalse(any(isinstance(b.type, types.InlineButtonTypeUrl) for row in markup.rows for b in row.buttons))
        self.assertEqual(set(self.session.choices.values()), {'ask:restart', 'home'})
        await self.click('⬅️ 返回面板')
        self.assertEqual(self.page.buttons[0][0].text, label)
        self.assertEqual(self.page.buttons[0][1].text, '📖 使用向导')
        self.assertIn('扫码登录', self.page.raw_text)

        for uid, chat in [(UID, -90), (71, 71)]:
            event = await self.text('/start', uid=uid, chat=chat)
            self.assertNotIn('buttons', event.respond.await_args.kwargs)

    async def test_download_uses_current_panel_for_completion(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        staging = self.bot.file_staging = FileStaging(self.bot, root, root)
        await self.click('📥 下载暂存')
        self.assertIsInstance(self.session.prompt.buttons, types.ReplyKeyboardForceReply)
        self.assertIn('请回复要下载的 Telegram 帖子链接', self.session.prompt.raw_text)
        self.assertTrue(self.session.sync_controls_mid)
        self.assertTrue(self.button('⬅️ 取消并返回'))
        self.assertTrue(self.button('查看文件任务'))
        await self.reply(f'https://t.me/c/{SOURCE}/99')
        await self.click('下载整组')
        job = staging.recent(UID)[0][0]
        current = self.page.id
        self.assertTrue(await self.bot.admin_panel.file_download_ready(UID, job, '下载完成，已暂存 2 项。'))
        self.assertEqual(self.page.id, current)
        self.assertTrue(self.button('暂存文件列表'))
        self.assertFalse(await self.bot.admin_panel.file_download_ready(UID, job, '重复回执'))

    async def test_protected_album_download_is_handled_by_tdl(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        staging = self.bot.file_staging = FileStaging(self.bot, root, root)
        posts = [photo(mid, group=123) for mid in (99, 100)]
        for post in posts:
            post.noforwards = True
        self.client.get_entity.return_value = NS(noforwards=True)
        self.client.get_messages.side_effect = lambda peer, *, ids: posts if isinstance(ids, list) else posts[0]

        async def write_media(link, path):
            Path(path).write_bytes(b'12345678')

        download = self.bot.tdl.download_media = AsyncMock(side_effect=write_media)
        link = f'https://t.me/c/{SOURCE}/99'
        job = staging.enqueue_download(UID, link, album=True)
        await staging.download(job, {'link': link, 'album': True})
        self.assertEqual([call.args[0] for call in download.await_args_list],
                         [link, f'https://t.me/c/{SOURCE}/100'])
        self.client.get_entity.assert_not_awaited()
        self.assertEqual(staging.files()[2], 2)
        self.assertEqual(staging.recent(UID)[0][2], 'done')

    async def test_file_cleanup_keeps_unselected_downloads(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        staging = self.bot.file_staging = FileStaging(self.bot, root, root)
        with closing(staging.connect()) as db, db:
            for mid in (1, 2):
                file_id = f'1-{mid}'
                (root / f'relay-{file_id}').write_bytes(b'file')
                db.execute('INSERT INTO files VALUES(?,1,?)',
                           (file_id, json.dumps({'name': f'{mid}.jpg', 'mime': 'image/jpeg', 'size': 4})))
        await self.click('📥 下载暂存')
        await self.click('暂存文件列表')
        await self.click('⬜ 1. 1.jpg')
        await self.click('删除所选')
        self.assertIn('将删除 1 项', self.page.raw_text)
        await self.click('确认删除')
        self.assertIsNone(staging.get('1-1'))
        self.assertFalse((root / 'relay-1-1').exists())
        self.assertIsNotNone(staging.get('1-2'))
        self.assertTrue((root / 'relay-1-2').is_file())

    async def test_staging_start_retires_legacy_publications_without_replaying(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        staging = self.bot.file_staging = FileStaging(self.bot, root, root)
        with closing(staging.connect()) as db, db:
            db.executemany("INSERT INTO jobs(owner,kind,state,payload) VALUES(?,?,?,'{}')",
                           [(UID, kind, state) for kind, state in [
                               ('publish', 'pending'), ('publish', 'working'), ('publish', 'sending'),
                               ('publish', 'done'), ('download', 'working')]])
        staging.start()
        try:
            jobs = [(kind, state) for _, kind, state, _ in reversed(staging.recent(UID))]
            self.assertEqual(jobs, [('publish', 'failed'), ('publish', 'failed'), ('publish', 'review'),
                                    ('publish', 'done'), ('download', 'pending')])
        finally:
            await staging.stop()

    async def test_webui_link_is_issued_only_from_admin_private_current_panel(self):
        panel = self.bot.admin_panel
        panel.webui_url = 'https://203.0.113.10:9443'
        await self.text('/start')
        root = self.page.id
        key = self.button('🌐 打开 WebUI')
        sessions = WebSessions()
        with patch('admin_panel.SESSIONS', sessions):
            for kwargs in (dict(uid=71, chat=71), dict(chat=-90), dict(mid=root + 100)):
                event = await self.hit(key, **kwargs)
                event.respond.assert_not_awaited()
            self.assertFalse(sessions.links)
            event = await self.click('🌐 打开 WebUI')
            event.respond.assert_awaited_once()
            url = event.respond.await_args.kwargs['buttons'][0][0].type.url
            self.assertTrue(url.startswith(panel.webui_url + '/#login='))
            code = url.split('#login=')[1]
            self.assertEqual(self.page.id, root)
            self.assertEqual(self.button('🌐 打开 WebUI'), key)
            self.assertTrue(sessions.redeem(code, panel.allowed)[0])
            self.assertEqual(sessions.redeem(code, panel.allowed), (None, None))

    async def test_home_pages_refresh_back_and_context(self):
        self.assertEqual(self.page.raw_text, self.bot.admin_panel.home_view()[0])
        self.assertEqual(len(self.page.buttons), 6)
        for label, expected in [('🎯 目标频道', str(TARGET)), ('📡 监听频道', str(SOURCE)),
                                ('📋 任务队列', '队列为空'), ('📝 转发记录', '暂无转发'),
                                ('🔗 源帖映射', '暂无已确认的源帖映射。'),
                                ('📖 使用向导', '①'), ('⚙️ 系统管理', '系统管理')]:
            with self.subTest(page=label):
                if label == '📝 转发记录':
                    self.bot.forward_log.get_all.return_value = []
                await self.click(label)
                self.assertIn(expected, self.page.raw_text)
                if label not in ('⚙️ 系统管理', '📖 使用向导'):
                    await self.click('🔄 刷新')
                    self.assertIn(expected, self.page.raw_text)
                await self.click('⬅️ 返回面板')
        self.assertEqual(self.client.send_message.await_count, 2)  # startup and replacement target page
        group = await self.text('/start', chat=-90)
        self.assertNotIn('buttons', group.respond.await_args.kwargs)
        user = await self.text('/start', uid=71, chat=71)
        self.assertNotIn('buttons', user.respond.await_args.kwargs)
        await self.text('/help')
        self.assertEqual(self.page.raw_text, app.HELP_MESSAGE_ADMIN)
        self.assertIn('TDL 转发账号：手动无引用转发或批量转发前，需要扫码登录', self.page.raw_text)
        self.assertIn('Telethon 用户会话：想用个人 Telegram 账号监听源频道时', self.page.raw_text)
        await self.click('⬅️ 返回面板')

    async def test_start_refreshes_the_startup_panel_without_sending_another(self):
        root = self.page
        await self.click('⚙️ 系统管理')
        first = await self.text('/start')
        await self.new(first)
        second = await self.text('/start')
        self.assertIs(self.page, root)
        self.assertEqual(root.raw_text, self.bot.admin_panel.home_view()[0])
        first.respond.assert_not_awaited()
        second.respond.assert_not_awaited()
        self.client.send_message.assert_awaited_once()
        await self.click('🎯 目标频道')

    async def test_start_sends_replacement_when_the_old_panel_cannot_be_edited(self):
        old_mid = self.session.mid
        with patch.object(self.client.edit_message, 'side_effect', app.errors.MessageIdInvalidError(None)):
            event = await self.text('/start')
        event.respond.assert_awaited_once()
        self.assertNotEqual(self.session.mid, old_mid)
        self.assertNotIn(old_mid, self.messages)
        self.assertEqual(self.page.raw_text, self.bot.admin_panel.home_view()[0])

    async def test_all_panel_commands_keep_one_message_per_admin(self):
        self.bot.forward_log.get_all.return_value = []
        with patch.object(app, 'ADMIN_IDS', [UID, 72]), patch.object(self.bot, 'schedule_command_delete') as cleanup:
            await self.text('/help', uid=72, chat=72)
            other = self.bot.admin_panel.sessions[72].mid
            for command in ('/help', '/start', '/flog', '/mappings', '/start'):
                await self.text(command)
                self.assertEqual(set(self.messages), {other, self.page.id})
            await self.click('🔄 手动同步')
            await asyncio.gather(self.text('/help'), self.text('/start'))
            self.assertEqual(set(self.messages), {other, self.page.id})
            self.assertEqual(self.messages[other].raw_text, app.HELP_MESSAGE_ADMIN)
            cleanup.assert_not_called()

    async def test_restart_reuses_page_and_recovers_interrupted_prompt_cleanup(self):
        async def restart():
            await self.bot.stop()
            with patch.object(app, 'TelegramClient', return_value=self.client):
                self.bot = app.TelegramBot()
            self.bot.target_chat_id = TARGET
            self.bot.task_queue.start = AsyncMock()
            self.addAsyncCleanup(self.bot.stop)
            await self.bot.start()
            self.new, self.cb = (self.client.handlers[k] for k in (events.NewMessage, events.CallbackQuery))

        original = self.page.id
        key = self.button('🎯 目标频道')
        await restart()
        self.assertEqual(list(self.messages), [original])
        self.assertTrue((await self.hit(key)).answer.await_args.kwargs['alert'])
        with patch.object(self.client, 'delete_messages', side_effect=ConnectionError('offline')), self.assertLogs('admin_panel', 'WARNING'):
            await self.click('🔄 手动同步')
        prompt = self.session.prompt_id
        controls = self.session.sync_controls_mid
        self.assertEqual(set(self.messages), {original, controls, prompt})
        await restart()
        self.assertEqual(list(self.messages), [self.page.id])
        self.assertNotIn(self.page.id, (original, controls, prompt))
        self.assertEqual(self.page.raw_text, self.bot.admin_panel.home_view()[0])

    async def test_button_owner_generation_and_message_binding(self):
        key = self.button('🎯 目标频道')
        for kw in (dict(uid=71, chat=71), dict(uid=71), dict(chat=-90), dict(mid=999), dict(via_inline=True)):
            ev = await self.hit(key, **kw)
            self.assertTrue(ev.answer.await_args.kwargs['alert'])
        with patch.object(app, 'ADMIN_IDS', [UID, 72]):
            ev = await self.hit(key, uid=72, chat=72)
            self.assertTrue(ev.answer.await_args.kwargs['alert'])
        with patch.object(app, 'ADMIN_IDS', []):
            ev = await self.hit(key)
            self.assertTrue(ev.answer.await_args.kwargs['alert'])
        ev = await self.hit(key + b'forged')
        self.assertTrue(ev.answer.await_args.kwargs['alert'])
        self.client.edit_message.assert_not_awaited()
        old_mid = self.session.mid
        await self.text('/start')
        self.client.edit_message.reset_mock()
        stale = await self.hit(key, mid=old_mid)
        self.assertTrue(stale.answer.await_args.kwargs['alert'])
        self.client.edit_message.assert_not_awaited()

    def assert_input_receipt(self, event, expected):
        event.respond.assert_awaited_once()
        call = event.respond.await_args
        self.assertEqual(call.kwargs['reply_to'], event.message.id)
        self.assertIn(expected, call.args[0])
        self.assertEqual(self.page.raw_text, call.args[0])

    async def test_replayed_commands_and_plain_submissions_are_only_handled_once(self):
        query = await self.text('/showto')
        await self.new(query)
        query.respond.assert_awaited_once()

        command = await self.text(f'/export_range {SOURCE} 100 110')
        await self.new(command)
        command.respond.assert_awaited_once()
        self.assertEqual(self.bot.task_queue.queue.qsize(), 1)

        link = f'https://t.me/c/{SOURCE}/7'
        submission = await self.text(link)
        await self.new(submission)
        self.bot.do_forward.assert_awaited_once_with(submission)
        await self.text(link)
        self.assertEqual(self.bot.do_forward.await_count, 2)

    async def test_sync_is_reply_only_and_mapped_edit_only(self):
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {7: 91})
        link = f'https://t.me/c/{SOURCE}/7'
        await self.click('🔄 手动同步')
        prompt = self.session.prompt_id
        self.assertIsInstance(self.messages[prompt].buttons, types.ReplyKeyboardForceReply)
        await self.text(link)
        self.bot.do_forward.assert_awaited_once()
        submitted = await self.reply(link)
        self.assert_input_receipt(submitted, '任务已入队，尚未完成同步。')
        await self.new(submitted)  # Telegram may deliver the same reply update again.
        self.assert_input_receipt(submitted, '任务已入队，尚未完成同步。')
        submitted.get_reply_message.assert_not_awaited()
        self.assertIn(f'https://t.me/c/{TARGET}/91', submitted.respond.await_args.args[0].split())
        q = self.bot.task_queue.queue
        self.assertEqual(q.qsize(), 1)
        task = q.get_nowait(); q.task_done()
        self.assertEqual((task.mode, task.source_chat_id, task.message_ids, task.target_chat_id),
                         ('monitor_edit', SOURCE, [7], TARGET))
        stale = await self.text(link, reply=prompt)
        self.assertIn('已失效', stale.respond.await_args.args[0])
        self.assertTrue(q.empty())
        self.bot.do_forward.assert_awaited_once()
        await self.click('⬅️ 返回面板'); await self.click('🔄 手动同步')
        missing = await self.reply(f'https://t.me/c/{SOURCE}/8')
        self.assert_input_receipt(missing, '没有转发映射')
        self.assertNotIn('https://t.me/', missing.respond.await_args.args[0])
        self.assertTrue(q.empty())

    async def test_sync_input_rejections_reply_to_the_submitted_message(self):
        link = f'https://t.me/c/{SOURCE}/7'
        for value, expected in [('不是链接', '链接无效'), ('取消', '已取消'), (link, '已改变'),
                                (link, '已过期'), (link, '不要转发消息')]:
            with self.subTest(expected=expected):
                await self.text('/start'); await self.click('🔄 手动同步')
                if expected == '已改变':
                    self.bot.target_chat_id += 1
                elif expected == '已过期':
                    self.session.expires = 1
                ev = await self.text(value, reply=self.session.prompt_id, forwarded=expected == '不要转发消息')
                self.assert_input_receipt(ev, expected)
                self.assertTrue(self.bot.task_queue.queue.empty())
                self.bot.do_forward.assert_not_awaited()

    async def test_sync_receipt_uses_queue_admission_result(self):
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {7: 91})
        for position, expected in [(None, '未加入同步队列'), (0, '任务已入队，尚未完成同步。')]:
            with self.subTest(position=position), patch.object(self.bot.task_queue, 'enqueue_edit',
                                                                new=AsyncMock(return_value=position)) as enqueue:
                await self.text('/start'); await self.click('🔄 手动同步')
                self.assert_input_receipt(await self.reply(f'https://t.me/c/{SOURCE}/7'), expected)
                enqueue.assert_awaited_once()

    async def test_sync_receipt_needs_no_panel_edit_and_does_not_replay(self):
        link = f'https://t.me/c/{SOURCE}/7'
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {7: 91})
        await self.click('🔄 手动同步')
        prompt = self.session.prompt_id
        self.client.edit_message.side_effect = ConnectionError('old panel deleted')
        ev = await self.text(link, reply=prompt)
        self.assert_input_receipt(ev, '任务已入队，尚未完成同步。')
        self.client.edit_message.assert_not_awaited()
        with patch.object(self.bot.task_queue, 'enqueue_edit', new_callable=AsyncMock) as enqueue:
            stale = await self.text(link, reply=prompt)
            self.assertIn('已失效', stale.respond.await_args.args[0])
            enqueue.assert_not_awaited()
        self.assertEqual(self.bot.task_queue.queue.qsize(), 1)
        self.bot.do_forward.assert_not_awaited()

    async def test_sync_receipt_send_failure_falls_back_to_panel(self):
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {7: 91})
        await self.click('🔄 手动同步')
        with patch.object(self, 'send', side_effect=ConnectionError('receipt failed')), self.assertLogs('admin_panel', 'WARNING'):
            ev = await self.reply(f'https://t.me/c/{SOURCE}/7')
        ev.respond.assert_awaited_once()
        self.assertIn('任务已入队，尚未完成同步。', self.page.raw_text)
        self.assertEqual(self.bot.task_queue.queue.qsize(), 1)
        self.assertFalse(self.session.prompt_id)

    async def test_old_prompt_never_becomes_submission_and_plain_reply_is_preserved(self):
        link = f'https://t.me/c/{SOURCE}/23'
        for reason in ('cancel', 'expired', 'restart', 'new_panel', 'unauthorized', 'forwarded'):
            with self.subTest(reason=reason):
                await self.text('/start'); await self.click('🔄 手动同步')
                mid = self.session.prompt_id
                if reason == 'cancel':
                    await self.reply('取消')
                elif reason == 'expired':
                    self.session.expires = 1
                elif reason == 'restart':
                    self.bot.admin_panel.sessions.clear()
                elif reason == 'new_panel':
                    await self.text('/help')
                if reason == 'unauthorized':
                    with patch.object(app, 'ADMIN_IDS', []):
                        await self.text(link, reply=mid)
                else:
                    await self.text(link, reply=mid, forwarded=reason == 'forwarded')
                self.bot.do_forward.assert_not_awaited()
                self.assertTrue(self.bot.task_queue.queue.empty())
        normal = self.send(UID, 'ordinary message')
        await self.text(link, reply=normal.id)
        self.bot.do_forward.assert_awaited_once()

    async def test_export_preview_and_concurrent_confirmation_admit_once(self):
        self.bot.channel_names.update({SOURCE: '素材库', TARGET: '归档频道'})
        await self.click('📦 批量转发')
        first = await self.reply(f'https://t.me/c/{SOURCE}/100')
        first.respond.assert_not_awaited()  # the second ForceReply prompt is already the receipt
        self.assertEqual(self.client.send_message.await_args.kwargs['reply_to'], first.message.id)
        self.assertIn('尚未入队', self.messages[self.session.prompt_id].raw_text)
        self.assertIn(f'当前目标：归档频道（{TARGET}）', self.messages[self.session.prompt_id].raw_text)
        last = await self.reply(f'https://t.me/c/{SOURCE}/110')
        self.assert_input_receipt(last, '待确认')
        for detail in (f'源频道：素材库（{SOURCE}）', f'目标频道：归档频道（{TARGET}）', '100 至 110'):
            self.assertIn(detail, self.page.raw_text)
        self.assertTrue(self.bot.task_queue.queue.empty())
        confirm = self.button('确认执行')
        clicks = await asyncio.gather(self.hit(confirm), self.hit(confirm))
        q = self.bot.task_queue.queue
        self.assertEqual(q.qsize(), 1)
        task = q.get_nowait(); q.task_done()
        self.assertEqual((task.mode, task.source_chat_id, task.message_ids, task.target_chat_id),
                         ('export_range', SOURCE, [100,110], TARGET))
        self.assertNotEqual(task.status_msg_id, self.session.mid)
        self.assertEqual(sum(click.respond.await_count for click in clicks), 1)
        status = self.messages[task.status_msg_id]
        self.assertIn('已加入范围导出队列', status.edit.await_args.args[0])
        self.assertEqual(self.page.raw_text, '转发进度见状态消息，也可在任务队列查看。')
        self.bot.do_forward.assert_not_awaited()

    async def test_export_cancel_returns_home_without_accepting_late_replies(self):
        for step in ('start', 'end', 'confirm'):
            with self.subTest(step=step):
                await self.click('📦 批量转发')
                if step != 'start':
                    await self.reply(f'https://t.me/c/{SOURCE}/100')
                prompt = self.session.prompt_id
                controls = self.session.sync_controls_mid
                if step == 'confirm':
                    await self.reply(f'https://t.me/c/{SOURCE}/110')
                await self.click('⬅️ 取消并返回')
                self.assertEqual(list(self.messages), [self.page.id])
                self.assertNotIn(prompt, self.messages)
                self.assertNotIn(controls, self.messages)
                self.assertEqual(self.session.start_link, '')
                self.assertEqual(self.page.raw_text, self.bot.admin_panel.home_view()[0])
                self.assertFalse(self.session.prompt_id)
                with patch.object(self.bot.admin_panel, '_error_notice', new_callable=AsyncMock) as notice:
                    await self.text(f'https://t.me/c/{SOURCE}/120', reply=prompt)
                    notice.assert_awaited_once()
                self.assertTrue(self.bot.task_queue.queue.empty())
                self.bot.do_forward.assert_not_awaited()

    async def test_invalid_export_and_changed_destination_are_never_admitted(self):
        for case in ('wrong_channel', 'reversed', 'bad_link', 'changed_before_confirm'):
            with self.subTest(case=case):
                await self.text('/start'); await self.click('📦 批量转发')
                await self.reply(f'https://t.me/c/{SOURCE}/100')
                dst = SOURCE + (case == 'wrong_channel')
                last = 99 if case == 'reversed' else 105
                await self.reply(f'https://t.me/c/{dst}/{last}' + ('?comment=3' if case == 'bad_link' else ''))
                if case == 'changed_before_confirm':
                    self.bot.target_chat_id += 1
                    await self.click('确认执行')
                    self.assertIn('已改变', self.page.raw_text)
                self.assertTrue(self.bot.task_queue.queue.empty())
                self.bot.do_forward.assert_not_awaited()

    async def test_target_and_monitor_changes_preview_and_share_command_state(self):
        home_mid = self.session.mid
        await self.click('🎯 目标频道')
        mid, old = self.session.mid, self.button('✏️ 更换目标')
        self.assertNotEqual(home_mid, mid)
        self.assertNotIn(home_mid, self.messages)
        await self.click('🔄 刷新')
        self.assertEqual(self.session.mid, mid)
        old = self.button('✏️ 更换目标')
        await self.click('✏️ 更换目标')
        self.assertNotIn(mid, self.messages)
        preview = await self.reply(str(TARGET+1))
        self.assert_input_receipt(preview, f'{TARGET} → {TARGET+1}')
        self.assertNotEqual(mid, self.session.mid)
        self.assertTrue((await self.hit(old, mid=mid)).answer.await_args.kwargs['alert'])
        self.assertEqual(self.bot.target_chat_id, TARGET)
        clicked = await self.click('确认执行')
        self.assertIn('已收到确认', clicked.answer.await_args.args[0])
        self.assertIn('目标频道已更新', self.page.raw_text)
        self.assertEqual(self.bot.target_chat_id, TARGET+1)
        await self.click('⬅️ 返回面板'); await self.click('📡 监听频道'); await self.click('➕ 添加')
        self.assert_input_receipt(await self.reply('12345'), '已添加自动监听源频道：12345')
        self.assertIn(12345, self.bot.monitor_chat_ids)
        await self.click('⬅️ 返回面板'); await self.click('📡 监听频道'); await self.click('替换整个列表')
        self.assert_input_receipt(await self.reply('123，456,123'), '123, 456')
        self.assertNotIn(456, self.bot.monitor_chat_ids)
        await self.click('确认执行'); self.assertEqual(self.bot.monitor_chat_ids, [123,456])
        await self.click('⬅️ 返回面板'); await self.click('📡 监听频道'); await self.click('➖ 点选移除')
        await self.click('123'); await self.click('确认执行'); self.assertEqual(self.bot.monitor_chat_ids, [456])
        await self.click('⬅️ 返回面板'); await self.click('📡 监听频道'); await self.click('清空监听…')
        cancelled = await self.click('取消')
        self.assertIn('已取消', cancelled.answer.await_args.args[0])
        self.assertEqual(self.bot.monitor_chat_ids, [456])
        await self.click('📡 监听频道'); await self.click('清空监听…'); await self.click('确认执行')
        self.assertEqual(self.bot.monitor_chat_ids, [])

    async def test_stop_clear_history_and_restart_require_fresh_confirmation(self):
        self.bot._restart = AsyncMock()
        self.bot.task_queue.stop_tdl_tasks = AsyncMock(return_value=(True, 2))
        self.bot.forward_log.get_all.return_value = []
        for page, control, op in [('📝 转发记录','🗑 清空本地记录…', self.bot.forward_log.clear),
                                  ('📋 任务队列','⏹ 停止任务…',self.bot.task_queue.stop_tdl_tasks),
                                  ('⚙️ 系统管理','🔄 重启机器人…',self.bot._restart)]:
            with self.subTest(action=control):
                await self.text('/start'); await self.click(page); await self.click(control)
                old = self.button('确认执行')
                self.session.expires = 1
                await self.hit(old)
                op.assert_not_called()
                await self.text('/start'); await self.click(page); await self.click(control)
                confirm = self.button('确认执行')
                await self.hit(old); op.assert_not_called()
                await asyncio.gather(self.hit(confirm), self.hit(confirm))
                op.assert_called_once()

    async def test_queue_clear_reviews_requires_fresh_confirmation(self):
        q = self.bot.task_queue
        await q.add_task(None, f'https://t.me/c/{SOURCE}/8', None, UID, target_chat_id=TARGET)
        task = q.queue.get_nowait(); q.queue.task_done(); q.journal.save(task, 'review')
        await q.add_task(None, f'https://t.me/c/{SOURCE}/9', None, UID, target_chat_id=TARGET)
        await self.text('/start'); await self.click('📋 任务队列')
        self.assertIn('排队中', self.page.raw_text)
        self.assertIn(f'#{task.job_id} clone', self.page.raw_text)
        await self.click('✅ 清空待核对…')
        old = self.button('确认执行')
        self.session.expires = 1
        await self.hit(old)
        self.assertEqual(q.journal.reviews()[0], 1)
        await self.text('/start'); await self.click('📋 任务队列'); await self.click('✅ 清空待核对…')
        await self.hit(old)
        self.assertEqual(q.journal.reviews()[0], 1)
        await self.click('确认执行')
        self.assertIn('已移除 1 项待核对记录', self.page.raw_text)
        self.assertEqual(q.journal.reviews()[0], 0)
        self.assertEqual(q.queue.qsize(), 1)

    async def test_input_is_not_a_command_and_cancel_never_submits(self):
        self.bot._restart = AsyncMock()
        for arg in ('/restart', '取消', f'https://t.me/c/{SOURCE}/8?comment=3'):
            await self.text('/start'); await self.click('🔄 手动同步')
            await self.reply(arg)
            self.assertTrue(self.bot.task_queue.queue.empty())
            self.bot.do_forward.assert_not_awaited()
            self.bot._restart.assert_not_awaited()
        await self.text('/start'); await self.click('📦 批量转发')
        invalid = await self.reply(f'https://t.me/c/{SOURCE}/2147483648')
        self.assertEqual(self.session.prompt_id, 0)
        self.assert_input_receipt(invalid, '无效')

    async def test_changed_monitor_list_and_authorization_in_flight(self):
        await self.click('📡 监听频道'); await self.click('清空监听…')
        self.bot.monitor_chat_ids.append(123)
        await self.click('确认执行')
        self.assertIn('已改变', self.page.raw_text)
        self.assertEqual(self.bot.monitor_chat_ids, [SOURCE, 123])
        await self.click('⬅️ 返回面板'); await self.click('📋 任务队列'); await self.click('⏹ 停止任务…')
        self.bot.task_queue.stop_tdl_tasks = AsyncMock()
        key = self.button('确认执行')
        with patch.object(app, 'ADMIN_IDS', []):
            await self.hit(key)
        self.bot.task_queue.stop_tdl_tasks.assert_not_awaited()

    async def test_remove_pagination_does_not_assume_channel_index(self):
        self.bot.monitor_chat_ids = list(range(11, 22))
        await self.click('📡 监听频道'); await self.click('➖ 点选移除'); await self.click('下一页')
        await self.click('20'); self.assertIn('移除 20', self.page.raw_text)
        await self.click('确认执行')
        self.assertNotIn(20, self.bot.monitor_chat_ids)
        self.assertIn(19, self.bot.monitor_chat_ids)

    async def test_feedback_failure_does_not_replay_a_completed_action(self):
        self.bot.forward_log.get_all.return_value = []
        await self.click('📝 转发记录'); await self.click('🗑 清空本地记录…')
        key, mid = self.button('确认执行'), self.session.mid
        self.client.edit_message.side_effect = ConnectionError('screen update failed')
        self.client.send_message.side_effect = ConnectionError('cannot deliver replacement')
        with self.assertLogs('admin_panel', 'ERROR'):
            await self.hit(key, mid=mid)
        self.assertNotIn(UID, self.bot.admin_panel.sessions)
        await self.hit(key, mid=mid)
        self.bot.forward_log.clear.assert_called_once()
        self.assertIn('核对当前状态', self.client.send_message.await_args.args[1])

    async def test_monitor_input_rejections_report_the_outcome_at_the_reply(self):
        for value, expected in [('not-an-id', '输入无效'), ('取消', '已取消'), ('123', '输入已过期')]:
            with self.subTest(expected=expected):
                await self.text('/start'); await self.click('📡 监听频道'); await self.click('➕ 添加')
                prompt = self.session.prompt_id
                if expected == '输入已过期':
                    self.session.expires = 1
                self.assert_input_receipt(await self.reply(value), expected)
                stale = await self.text('123', reply=prompt)
                self.assertIn('已失效', stale.respond.await_args.args[0])
                self.assertEqual(self.bot.monitor_chat_ids, [SOURCE])
                self.bot.do_forward.assert_not_awaited()

    async def test_confirmation_reply_failure_sends_an_actionable_confirmation_panel(self):
        await self.click('🎯 目标频道')
        old_mid = self.session.mid
        await self.click('✏️ 更换目标')
        with patch.object(self, 'send', side_effect=ConnectionError('receipt failed')), self.assertLogs('admin_panel', 'WARNING'):
            await self.reply(str(TARGET+1))
        self.assertNotIn(old_mid, self.messages)
        self.assertNotEqual(self.session.mid, old_mid)
        self.assertIn('尚未执行', self.page.raw_text)
        self.assertEqual(self.bot.target_chat_id, TARGET)
        await self.click('确认执行')
        self.assertEqual(self.bot.target_chat_id, TARGET+1)

    async def test_missing_confirmation_card_replies_with_actual_result_without_replay(self):
        self.bot.forward_log.get_all.return_value = []
        await self.click('📝 转发记录'); await self.click('🗑 清空本地记录…')
        old_mid, key = self.session.mid, self.button('确认执行')
        self.client.edit_message.side_effect = ConnectionError('card gone')
        with self.assertLogs('admin_panel', 'WARNING'):
            await self.hit(key)
        self.assertNotEqual(self.session.mid, old_mid)
        self.assertIn('已清除所有转发记录', self.page.raw_text)
        self.assertTrue(self.button('⬅️ 返回面板'))
        self.assertTrue((await self.hit(key, mid=old_mid)).answer.await_args.kwargs['alert'])
        self.bot.forward_log.clear.assert_called_once()

    async def test_standalone_id_gets_guidance_without_consuming_prompt_or_rerouting_posts(self):
        (await self.text('12345')).respond.assert_not_awaited()
        await self.click('📡 监听频道'); await self.click('➕ 添加')
        prompt = self.session.prompt_id
        ev = await self.text('12345')
        self.assertIn('没有回复到输入提示', ev.respond.await_args.args[0])
        self.assertEqual(ev.respond.await_args.kwargs['reply_to'], ev.message.id)
        self.assertEqual(self.session.prompt_id, prompt)
        self.assertNotIn(12345, self.bot.monitor_chat_ids)
        await self.text(f'https://t.me/c/{SOURCE}/8')
        self.bot.do_forward.assert_awaited_once()
        self.assert_input_receipt(await self.reply('12345'), '已添加')

    async def test_prompt_expiry_deletes_prompt_and_sends_an_expiry_panel(self):
        root = self.page
        with patch('admin_panel.TTL', 0.02):
            await self.click('📦 批量转发')
            prompt = self.session.prompt
            await asyncio.wait_for(self.session.timer, 2)
        self.assertNotIn(prompt.id, self.messages)
        self.assertNotIn(root.id, self.messages)
        self.assertNotEqual(prompt.id, self.page.id)
        self.assertIn('过期', self.page.raw_text)
        self.assertEqual(self.session.prompt_id, 0)
        self.assertTrue(self.button('⬅️ 返回面板'))
        self.assertTrue(self.bot.task_queue.queue.empty())

    async def test_confirmation_timeout_updates_current_card_after_old_panel_was_deleted(self):
        root = self.page
        await self.click('🎯 目标频道'); await self.click('✏️ 更换目标')
        with patch('admin_panel.TTL', 0.02):
            await self.reply(str(TARGET+1))
            card, confirm = self.page, self.button('确认执行')
            await asyncio.wait_for(self.session.timer, 2)
        self.assertNotIn(root.id, self.messages)
        self.assertIs(card, self.page)
        card.delete.assert_not_awaited()
        self.assertIn('过期', self.page.raw_text)
        self.assertTrue((await self.hit(confirm, mid=card.id)).answer.await_args.kwargs['alert'])
        self.assertEqual(self.bot.target_chat_id, TARGET)

        self.assertTrue(self.button('⬅️ 返回面板'))

    async def test_used_prompt_and_retired_card_cleanup_never_targets_active_panel(self):
        with patch.object(self.bot, 'schedule_delete', wraps=self.bot.schedule_delete) as schedule:
            await self.click('🎯 目标频道'); await self.click('✏️ 更换目标')
            prompt = self.session.prompt
            first_timer = self.session.timer
            await self.reply(str(TARGET+1))
            self.assertNotIn(prompt.id, self.messages)
            card, wait = self.page, self.session.timer
            await self.click('确认执行')
            await asyncio.sleep(0)
            self.assertTrue(first_timer.cancelled() and wait.cancelled())
            self.assertIs(card, self.page)
            await self.text('/help')
            self.assertEqual(list(self.messages), [card.id])
            self.assertEqual(card.raw_text, app.HELP_MESSAGE_ADMIN)
            schedule.assert_not_called()

    async def test_receipt_retention_by_outcome_and_sync_override(self):
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {7: 91})
        link = f'https://t.me/c/{SOURCE}/7'
        with patch.object(self.bot, 'schedule_delete', wraps=self.bot.schedule_delete) as schedule:
            for button, text, delay in [('➕ 添加', '12345', 120), ('➕ 添加', '取消', 120),
                                        ('➕ 添加', 'invalid', 600), ('🔄 手动同步', link+'1', 600),
                                        ('🔄 手动同步', link, 0), ('🔄 手动同步', link, 17)]:
                with self.subTest(text=text, delay=delay), patch.object(app, 'AUTO_DELETE_SYNC_RECEIPT_SECONDS',
                                                                        17 if delay == 17 else 0):
                    await self.text('/start')
                    if button == '➕ 添加':
                        await self.click('📡 监听频道')
                    await self.click(button)
                    ev = await self.reply(text)
                    receipt, actual_delay = schedule.call_args.args
                    self.assertEqual(actual_delay, delay)
                    self.assertEqual(receipt.raw_text, ev.respond.await_args.args[0])
                    self.assertIs(receipt, self.page)
                    self.assertNotEqual(receipt.id, ev.message.id)

    async def test_cleanup_switch_off_preserves_expiry_checks_without_any_timers(self):
        with patch.object(app, 'AUTO_DELETE_BOT_MESSAGES', False):
            await self.click('🎯 目标频道'); await self.click('✏️ 更换目标')
            prompt = self.session.prompt
            self.assertIsNone(self.session.timer)
            await self.reply(str(TARGET+1))
            card = self.page
            self.assertIsNone(self.session.timer)
            self.session.expires = 1
            await self.click('确认执行')
            self.assertIn('过期', self.page.raw_text)
            self.assertEqual(self.bot.target_chat_id, TARGET)
            await self.text('/start')
            self.assertFalse(self.bot._delete_tasks)
            prompt.delete.assert_not_awaited()
            card.delete.assert_not_awaited()
