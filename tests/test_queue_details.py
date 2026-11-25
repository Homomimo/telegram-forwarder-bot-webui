"""Offline coverage for queue visibility and review acknowledgement."""
import asyncio
import re
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from bot_fixture import MonitorHarness, SOURCE, TARGET, app


class QueueDetailsTests(MonitorHarness, unittest.IsolatedAsyncioTestCase):
    async def command(self, *args):
        respond = AsyncMock()
        with patch.object(app, 'ADMIN_IDS', [61]):
            self.assertTrue(await self.bot.run_admin_action(NS(sender_id=61), list(args), respond))
        return '\n'.join(call.args[0] for call in respond.await_args_list)

    @property
    def queue(self):
        return self.bot.task_queue

    def take(self):
        task = self.queue.queue.get_nowait()
        self.queue.queue.task_done()
        return task

    async def admit(self, mid, *, link=None, **kwargs):
        await self.queue.add_task(None, link or f'https://t.me/c/{SOURCE}/{mid}', None, 61,
                                  target_chat_id=TARGET, **kwargs)

    async def detached(self, mid, state='review', **kwargs):
        await self.admit(mid, **kwargs)
        task = self.take()
        self.queue.journal.save(task, state)
        return task

    async def test_empty_notice_unchanged(self):
        self.assertEqual(await self.command('/queue'), '📋 转发队列为空，当前没有正在处理或等待中的任务。')

    async def test_worker_and_fifo_details(self):
        q = self.queue
        for mid in (20, 1, 5):
            await self.admit(mid, link=f'https://t.me/c/{SOURCE}/{mid}?single')
        first, second, third = [item['job_id'] for item in q.snapshot()['queued']]
        started = asyncio.Event()

        async def block(task):
            started.set()
            await asyncio.Future()

        with patch.object(q, '_process_task', side_effect=block):
            await q.start()
            await asyncio.wait_for(started.wait(), 2)
            self.assertGreater(q.current_task.started_at, 0)
            q.current_task.started_at -= 72
            text = await self.command('/queue')
        self.assertIn(f'正在处理 #{first} 手动转发 · 已运行 1分12秒', text)
        self.assertIn(f'目标：{TARGET} · 消息数：1 条', text)
        self.assertEqual(re.findall(r'^\d+\. #(\d+) 手动转发', text, re.M), [str(second), str(third)])
        self.assertIn('共 2 项：排队 2', text)
        self.assertNotIn('定时重试：', text)

    async def test_live_categories_are_counted_and_snapshot_is_read_only(self):
        q = self.queue
        retry = await self.detached(1, state='pending', mode='monitor_edit', source_chat_id=SOURCE, message_ids=[1])
        q.retry_later(retry, 30)
        with patch.object(app, 'MONITOR_ALBUM_WAIT', 30):
            for mid in (2, 3):
                await self.admit(mid, mode='monitor_copy', source_chat_id=SOURCE,
                                 message_ids=[mid], grouped_id=10, album_member=True)
        for mid in (4, 5):
            q.note_early_edit(app.ForwardTask(None, f'early-only-{mid}', None, 61, TARGET,
                                              mode='monitor_edit', source_chat_id=SOURCE, message_ids=[mid]))
        peer = app.ForwardTask(None, 'peer-source', None, 61, TARGET, mode='monitor_copy', job_id=700)
        album = app.ForwardTask(None, 'album-source', None, 61, TARGET, mode='monitor_copy', job_id=701,
                                waiting_for_album=True)
        store = app.ForwardTask(None, 'store-source', None, 61, TARGET, mode='monitor_copy', job_id=702)
        q._peer_wait.update({peer.job_id: peer, album.job_id: album})
        q._store_wait[store.job_id] = store
        q.current_task = app.ForwardTask(None, 'edit-source', None, 61, TARGET, mode='monitor_edit', job_id=703,
                                         followup=app.ForwardTask(None, 'followup-source', None, 61, TARGET))
        await self.admit(100, link=f'chat_id:{SOURCE}', mode='export_range', source_chat_id=SOURCE, message_ids=[100, 200])

        with patch.object(q, 'journal', None):
            snap = q.snapshot()
            self.assertEqual(q.snapshot(0)['retry'], [])
        self.assertEqual(snap['counts'], dict(queued=1, retry=1, albums=1, peer=2, store=1, early_edits=2, followup=1))
        self.assertEqual(sum(snap['counts'].values()), q.get_queue_status()['queue_size'])
        self.assertEqual(snap['albums'][0]['count'], 2)
        self.assertNotIn('event', snap['current_task'])
        self.assertNotIn('status_client', snap['current_task'])
        self.assertNotIn('rejected_copy', snap['retry'][0])
        text = await self.command('/queue')
        self.assertIn(f'共 {q.get_queue_status()["queue_size"]} 项', text)
        self.assertRegex(text, rf'定时重试：\n#{retry.job_id} 自动编辑 · 第 1 次 · (?:29|30) 秒后')
        self.assertRegex(text, r'收集相册：\n#\d+ 已收 2 条')
        self.assertIn(f'批量转发 100–200 · https://t.me/c/{SOURCE}/100', text)
        self.assertIn('#700 自动复制 · 等待频道实体', text)
        self.assertIn('#701 自动复制 · 等待相册补齐', text)
        self.assertIn('解锁后重启可恢复', text)
        self.assertIn('待映射编辑：2 项', text)
        self.assertIn('另有 1 次后续编辑已合并', text)
        self.assertNotIn('early-only-', text)
        self.assertNotIn('followup-source', text)

    async def test_length_budget_keeps_review_cursor_and_omission_count(self):
        reviews = [await self.detached(mid, link='https://t.me/' + '🟩' * 200,
                                       mode='monitor_copy', source_chat_id=SOURCE, message_ids=list(range(1, 11)))
                   for mid in range(1, 7)]
        for mid in range(1, 51):
            await self.admit(mid, link=f'https://t.me/c/{SOURCE}/{mid}' + '🟩' * 200)
        for i in range(5):
            item = app.ForwardTask(None, '🟩' * 200, None, 61, TARGET, mode='monitor_edit',
                                   job_id=100 + i, source_chat_id=SOURCE, message_ids=[100 + i])
            self.queue._store_wait[item.job_id] = item
        text = await self.command('/queue')
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 3800)
        fifo = text.split('⏳ 排队中', 1)[1].split('\n\n', 1)[0]
        shown = re.findall(r'^\d+\. #', fifo, re.M)
        self.assertLess(len(shown), 10)  # Multiple populated sections force whole FIFO rows out.
        self.assertIn(f'…还有 {50 - len(shown)} 项', fifo)
        self.assertEqual(len(re.findall(r'^#\d+ monitor_copy：', text, re.M)), 5)
        self.assertIn(f'下一页：/queue {reviews[1].job_id}', text)
        self.assertIn('核对完成后：/queue done', text)

    async def test_done_validates_all_ids_and_lists_unhandled(self):
        q = self.queue
        a, b, c = [await self.detached(mid) for mid in range(1, 4)]
        for args in ((), (str(a.job_id), 'x'), ('all', str(b.job_id)), ('0',), ('9223372036854775808',)):
            with self.subTest(args=args):
                self.assertIn('用法', await self.command('/queue', 'done', *args))
                self.assertEqual(q.journal.reviews()[0], 3)
        text = await self.command('/queue', 'done', str(a.job_id), str(a.job_id), '999')
        self.assertIn('已移除 1 项待核对记录', text)
        self.assertIn('：#999', text)
        self.assertIn('未删除频道消息，也不会重新发送', text)
        self.assertEqual([r['job_id'] for r in q.journal.reviews()[1]], [c.job_id, b.job_id])
        self.assertIn('已移除 2 项', await self.command('/queue', 'done', 'all'))
        self.assertEqual(q.journal.reviews()[0], 0)

    async def test_done_all_protects_current_and_non_review_states(self):
        q = self.queue
        await self.detached(1)
        protected = [await self.detached(mid, state) for mid, state in enumerate(
            ('pending', 'sending', 'retry_rejected', 'early', 'wait_store', 'wait_peer', 'wait_album'), 2)]
        current = q.current_task = await self.detached(10)
        self.assertIn('已移除 1 项', await self.command('/queue', 'done', 'all'))
        self.assertEqual([(jid, state) for jid, state, _ in q.journal.restore()],
                         list(zip([task.job_id for task in protected] + [current.job_id],
                                  ['pending', 'sending', 'retry_rejected', 'early', 'wait_store', 'wait_peer', 'wait_album', 'review'])))
        text = await self.command('/queue', 'done', str(current.job_id), str(protected[2].job_id))
        self.assertIn('已移除 0 项', text)
        self.assertIn(f'#{current.job_id} #{protected[2].job_id}', text)
        q.current_task = None

    async def test_done_keeps_copy_intent_and_duplicate_is_not_sent(self):
        client = self.bot.client
        client.forward.side_effect = TimeoutError('missing receipt')
        await self.enqueue(self.event(12))
        with self.assertLogs(app.logger, 'ERROR'):
            task = await self.process()
        self.assertEqual(task.status, app.TaskStatus.UNCONFIRMED)
        self.assertEqual(self.queue.journal.reviews()[0], 1)
        db = self.bot.monitor_message_map().db
        intents = db.execute('SELECT * FROM monitor_copy_intents').fetchall()
        self.assertEqual(len(intents), 1)
        self.assertIn('已移除 1 项', await self.command('/queue', 'done', str(task.job_id)))
        self.assertEqual(self.queue.journal.reviews()[0], 0)
        self.assertEqual(db.execute('SELECT * FROM monitor_copy_intents').fetchall(), intents)
        client.forward.reset_mock()
        await self.enqueue(self.event(12))
        self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        client.forward.assert_not_awaited()

    async def test_tdlstop_preserves_reviews(self):
        reviewed = await self.detached(1)
        await self.admit(2)
        text = await self.command('/tdlstop')
        self.assertIn('清空等待任务数：1', text)
        self.assertIn('待核对记录不受影响，请用 /queue done 处理', text)
        self.assertEqual([r['job_id'] for r in self.queue.journal.reviews()[1]], [reviewed.job_id])
