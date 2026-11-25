"""No network: exercise the real manual queue, on-disk history, journal and request types."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telethon import errors, functions, types, utils
from bot_fixture import FakeClient, MonitorHarness, SOURCE, TARGET, app
from forward_records import counts, history_detail, history_list
from manual_forward import read_export

ForwardLog = app.ForwardLog
BASE = f'https://t.me/c/{SOURCE}'


class ManualTests(MonitorHarness, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.history = Path(self.bot.task_queue.journal.path).with_name('history.json')
        self.bot.forward_log = ForwardLog(self.history)
        self.client = self.bot.client
        self.ready(self.client)
        self.bot.tdl.forward_clone = AsyncMock(return_value=(False, app.UnconfirmedReceipt('没有逐条回执')))
        self.bot.tdl.forward_from_export = AsyncMock(side_effect=AssertionError('unexpected CLI resend'))
        self.q = self.bot.task_queue

    def ready(self, client):
        client.get_entity.return_value = types.Channel(TARGET, 'target', types.ChatPhotoEmpty(), None,
                                                      broadcast=True, creator=True)
        self.posts = {i: types.Message(i, types.PeerChannel(SOURCE), message=f'text {i}', grouped_id=g)
                      for i, g in [(10, 33), (11, None), (12, 33), (15, 33)]}
        async def read(peer, ids):
            return self.posts.get(ids) if isinstance(ids, int) else [self.posts[i] for i in ids if i in self.posts]
        client.get_messages.side_effect = read

    async def submit(self, suffix):
        await self.q.add_task(None, BASE + suffix, None, 1, target_chat_id=utils.get_peer_id(types.PeerChannel(TARGET)))
        task = await self.process()
        return task, ForwardLog(self.history).get_all()[-1]

    async def enqueue_range(self, ids=(15, 12, 10, 12)):
        async def export(source, first, last, path, progress):
            Path(path).write_text(json.dumps({'id': source, 'messages': [{'id': i} for i in ids]}))
            return True, ''
        self.bot.tdl.export_range = export
        await self.q.add_task(None, f'chat_id:{SOURCE}', None, 1, mode='export_range', source_chat_id=SOURCE,
                              target_chat_id=TARGET, message_ids=[10, 15])

    async def test_manual_receipt_uses_snapshotted_target_and_separate_links(self):
        status = NS(edit=AsyncMock())
        self.bot.channel_names.update({SOURCE: '素材__库', TARGET: '归档 [A]'})
        self.bot.target_chat_id = TARGET + 1
        with patch.object(self.q, '_get_status', AsyncMock(return_value=status)):
            await self.enqueue_range()
            await self.process()
        text = status.edit.await_args.args[0]
        self.assertIn(f'来源：素材__库（{SOURCE}）\n目标：归档 [A]（{TARGET}）', text)
        self.assertIn(f'源帖：\n{BASE}/10\n范围：10-15', text)
        self.assertIn('目标帖：\nhttps://t.me/c/', text)
        self.assertIsNone(status.edit.await_args.kwargs['parse_mode'])
        self.assertNotIn('chat_id:', text)

    def test_unresolved_public_source_uses_username_not_an_invented_id(self):
        link = 'https://t.me/channel_name/42?single'
        self.assertEqual(self.bot.transfer_context(link, TARGET),
                         f'来源：@channel_name\n目标：{TARGET}\n源帖：\n{link}')

    async def test_album_partial_reversed_receipts_and_links(self):
        async def rpc(req):
            self.assertEqual(req.id, [10, 12, 15])  # never include interleaved post 11
            return NS(updates=[types.UpdateMessageID(801, req.random_id[1]), types.UpdateMessageID(822, req.random_id[0])])
        self.client.forward.side_effect = rpc
        task, row = await self.submit('/12')
        self.assertEqual(task.status, app.TaskStatus.UNCONFIRMED)
        self.assertEqual(row['members'], [[10, 822], [12, 801], [15, 0]])
        self.assertIn('涉及 3 · 确认送达 2 · 明确失败 0 · 待核对 1', counts(row))
        text, _ = history_detail(row, 1, 1)
        self.assertIn(f'https://t.me/c/{TARGET}/822?single', text)
        self.assertIn('源消息 15 → 待核对，无目标链接', text)
        self.assertEqual(text.count('href='), 2)
        self.assertEqual(self.q.journal.reviews()[0], 1)
        self.bot.tdl.forward_clone.assert_not_awaited()

    async def test_single_and_repeat_do_not_overwrite_auto_mapping(self):
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {10: 90})
        seq = iter([300, 430]); nonces = []
        async def rpc(req):
            self.assertEqual(req.id, [10]); nonces.append(req.random_id[0])
            return NS(updates=[types.UpdateMessageID(next(seq), req.random_id[0])])
        self.client.forward.side_effect = rpc
        _, row = await self.submit('/10?single'); _, again = await self.submit('/10?single')
        self.assertNotEqual(row['id'], again['id']); self.assertNotEqual(*nonces)
        self.assertEqual([row['members'], again['members']], [[[10, 300]], [[10, 430]]])
        self.assertIn(f'href="https://t.me/c/{TARGET}/430"', history_list([again], 1)[0])
        self.assertEqual(self.bot.monitor_message_map().get(SOURCE, 10, TARGET), 90)

    async def test_range_timeout_preserves_receipts_and_unsent_tail(self):
        await self.enqueue_range()
        async def rpc(req):
            if req.id == [12]:
                raise TimeoutError('lost reply')
            self.assertEqual(req.id, [10])
            return NS(updates=[types.UpdateMessageID(850, req.random_id[0])])
        self.client.forward.side_effect = rpc
        task = await self.process()
        self.assertEqual(task.status, app.TaskStatus.UNCONFIRMED)
        row, = ForwardLog(self.history).get_all()
        self.assertEqual(row['members'], [[10, 850], [12, 0], [15, -1]])
        self.assertEqual(row['route'], 'Bot 发布')  # Using TDL for export does not make it the sender.
        self.assertFalse(row['album']); self.assertIn('10–15', row['from'])
        self.bot.tdl.forward_from_export.assert_not_awaited()

    async def test_only_tdl_has_access_and_no_guessed_member_count(self):
        self.client.get_entity.return_value.creator = False
        _, row = await self.submit('/10')
        self.assertEqual(row['route'], '手动转发（TDL 账号发送） · 无逐条成功回执'); self.assertIsNone(row['members'])
        self.assertIn('条数未知', counts(row)); self.assertNotIn('href=', history_list([row], 1)[0])
        _, row = await self.submit('/10?single'); self.assertEqual(row['members'], [[10, 0]])
        self.client.forward.assert_not_awaited(); self.assertEqual(self.bot.tdl.forward_clone.await_count, 2)

    async def test_cli_diagnostics_are_not_proof_of_failure_for_timeouts_or_other_posts(self):
        link = f'{BASE}/10?single'
        for source, mid, target, diag, pending in [(SOURCE,10,TARGET,'deadline exceeded',True),
                (SOURCE,10,TARGET+1,'CHAT_WRITE_FORBIDDEN',True), (SOURCE,11,TARGET,'denied',True),
                (SOURCE,10,TARGET,'RPC error 403: CHAT_WRITE_FORBIDDEN',False)]:
            with self.subTest(diag=diag, mid=mid, target=target):
                line = f'S({source}):{mid} -> T({target}) error: {diag}'
                with patch.object(self.bot.tdl, '_run_command', return_value=(0, line, '')):
                    ok, output = await app.TDLDownloader.forward_clone(self.bot.tdl, link, TARGET)
                self.assertFalse(ok); self.assertEqual(isinstance(output, app.UnconfirmedReceipt), pending)

    async def test_cli_counts_definite_failures_beyond_the_log_sample(self):
        ids = list(range(1, 61))
        async def run(cmd, progress, *, on_record):
            for mid in ids:
                on_record(f'S({SOURCE}):{mid} -> T({TARGET}) error: CHAT_WRITE_FORBIDDEN')
            return 0, '', ''
        with patch.object(self.bot.tdl, '_run_command', side_effect=run):
            _, diag = await app.TDLDownloader.forward_from_export(self.bot.tdl, 'file', TARGET, scope=(SOURCE, set(ids)))
        self.assertEqual(diag.failed_members, set(ids))
        self.assertIn('仅列前 32 条', diag)

    async def test_explicit_tdl_never_uses_authorized_bot(self):
        with patch.object(app, 'MANUAL_FORWARD_MODE', 'tdl'):
            _, row = await self.submit('/10?single')
        self.assertEqual(row['members'], [[10, 0]])
        self.client.get_entity.assert_not_awaited(); self.client.forward.assert_not_awaited()

    async def test_user_role_preflight_and_public_username(self):
        other = FakeClient(); self.ready(other); self.bot.user_client = other; self.bot.user_monitor_running = True
        self.client.get_input_entity.side_effect = errors.ChannelPrivateError(None)
        async def resolve(peer):
            return types.InputPeerChannel(SOURCE if isinstance(peer, str) else peer.channel_id, 44)
        other.get_input_entity.side_effect = resolve
        await self.q.add_task(None, 'https://t.me/testchannel/11', None, 1, target_chat_id=TARGET)
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        self.assertEqual(ForwardLog(self.history).get_all()[0]['route'], '用户账号发布')
        req = other.forward.await_args.args[0]; self.assertEqual(req.from_peer.access_hash, 44)
        self.client.forward.assert_not_awaited()

    async def test_restricted_explicit_rejection_can_publish_once_with_receipt(self):
        async def rpc(req):
            if isinstance(req, functions.messages.ForwardMessagesRequest):
                raise errors.ChatForwardsRestrictedError(None)
            self.assertIsInstance(req, functions.messages.SendMessageRequest)
            return NS(updates=[types.UpdateMessageID(222, req.random_id)])
        self.client.forward.side_effect = rpc
        task, row = await self.submit('/11')
        self.assertEqual(task.status, app.TaskStatus.COMPLETED); self.assertEqual(row['members'], [[11, 222]])
        self.bot.tdl.forward_clone.assert_not_awaited()

    async def test_restart_after_receipt_but_before_json_finalization(self):
        await self.enqueue_range()
        with patch.object(self.bot.forward_log, 'flush', return_value=False):
            await self.process()
        self.assertEqual(self.q.journal.restore()[0][1], 'sending')
        # Simulate losing process memory before the JSON writer succeeds.
        self.bot.forward_log = ForwardLog(self.history)
        self.q._loaded = False; self.q._restore()
        row, = self.bot.forward_log.get_all()
        self.assertEqual(row['members'], [(10, 730), (12, 736), (15, 745)])
        self.assertTrue(self.q.queue.empty()); self.assertEqual(self.q.journal.restore(), [])
        self.assertEqual(self.client.forward.await_count, 3)

    async def test_restart_between_json_flush_and_journal_cleanup_does_not_duplicate(self):
        await self.enqueue_range()
        with patch.object(self.q, '_finish_manual'):
            await self.process()
        self.bot.forward_log = ForwardLog(self.history)
        self.q._loaded = False
        self.q._restore()
        row, = self.bot.forward_log.get_all()
        self.assertEqual(row['members'], [[10, 730], [12, 736], [15, 745]])
        self.assertEqual(self.q.journal.restore(), [])
        self.assertEqual(self.client.forward.await_count, 3)

    async def test_stop_mid_range_does_not_lose_receipts_or_replay(self):
        await self.enqueue_range()
        entered = asyncio.Event()
        async def rpc(req):
            if req.id == [12]:
                entered.set(); await asyncio.Future()
            return NS(updates=[types.UpdateMessageID(600, req.random_id[0])])
        self.client.forward.side_effect = rpc
        await self.q.start(); await asyncio.wait_for(entered.wait(), 3)
        await self.q.stop_tdl_tasks()
        row, = ForwardLog(self.history).get_all()
        self.assertEqual(row['members'], [[10, 600], [12, 0], [15, -1]])
        self.assertEqual(self.q.journal.reviews()[0], 1)
        self.bot.tdl.forward_from_export.assert_not_awaited()


class HistoryTests(unittest.TestCase):
    def test_old_automatic_route_does_not_infer_downloader_or_publisher(self):
        row = dict(id='22', date='date', to=TARGET, success=True, route='TDL 回退上传', **{'from': 'source'})
        text, _ = history_detail(row, 1, 1)
        self.assertIn('发送方式：重新发布（旧记录未区分媒体和发布账号）', text)
        self.assertNotIn('TDL', text)
        self.assertEqual(row['route'], 'TDL 回退上传')

    def test_export_bounds_channel_count_and_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'export.json')
            for channel, ids in [(SOURCE+1,[10]), (SOURCE,[9,10]), (SOURCE,[10,16]), (SOURCE,[True])]:
                path.write_text(json.dumps(dict(id=channel, messages=[dict(id=i) for i in ids])))
                with self.assertRaises(ValueError): read_export(path, SOURCE, 10, 15)
            path.write_text(json.dumps(dict(id=SOURCE, messages=[dict(id=i) for i in [15, 10, 15]])))
            self.assertEqual(read_export(path, SOURCE, 10, 15), [10,15])
            self.assertEqual(len(json.loads(path.read_text())['messages']), 2)

    def test_detail_pages_keep_every_member_and_end_of_diagnostics(self):
        row = dict(id='22', success=False, date='date', to=TARGET, error='🟩<&>'*4000 + 'END123',
                   members=[(i, i+1000) for i in range(1,400)], **{'from': 'source'})
        pages = []
        for page in range(1, 1000):
            text, keys = history_detail(row, 4, page)
            pages.append(text)
            self.assertLessEqual(len(text.encode('utf-16-le'))//2, 4096)
            self.assertEqual(keys[-1][0][1], 'hpage:4')
            if not any(label=='下一页' for buttons in keys for label, _ in buttons): break
        all_text = ''.join(pages)
        self.assertEqual(all_text.count('href='), 399)
        self.assertEqual(all_text.count('&lt;'), 4000); self.assertIn('END123', pages[-1]); self.assertNotIn('<&>', all_text)
