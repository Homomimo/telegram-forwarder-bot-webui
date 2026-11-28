"""Private admin UI. Callback payloads are opaque; only the current screen owns its actions."""
import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field, replace
from functools import partial
from html import escape

from telethon import Button, errors, types
from file_staging import PAGE_SIZE as FILE_PAGE_SIZE, parse_link, size_label
from forward_records import PAGE_SIZE, history_list, history_detail
from web_sessions import LINK_TTL, SESSIONS

logger = logging.getLogger(__name__)

PREFIX = b'ap1:'
PROMPT = '⌨️ 管理面板输入｜'
TTL = 300
HOME = [[('📖 使用向导', 'help')],
        [('📥 下载暂存', 'files:downloads')],
        [('🎯 目标频道', 'target'), ('📡 监听频道', 'monitor')],
        [('🔄 手动同步', 'input:sync'), ('📦 批量转发', 'input:export')],
        [('📋 任务队列', 'queue'), ('📝 转发记录', 'history')],
        [('🔗 源帖映射', 'mappings:1'), ('⚙️ 系统管理', 'system')]]
BACK = [('⬅️ 返回面板', 'home')]
PAGES = {
    'target': ('/showto', [[('✏️ 更换目标', 'input:target')]]),
    'monitor': ('/monitor', [[('🔀 频道配对', 'pairs:1')], [('➕ 添加', 'input:add'), ('➖ 点选移除', 'remove:0')]]),
    'queue': ('/queue', [[('⏹ 停止任务…', 'ask:stop'), ('✅ 清空待核对…', 'ask:reviewclear')]]),
}
QUESTIONS = {
    'sync': '请粘贴源频道原帖链接（如 https://t.me/c/1234567890/42）。机器人会回复是否已入队。',
    'export': '第 1 步：请回复起始原帖的 t.me/c 消息链接。',
    'end': '已收到起始链接，尚未入队。第 2 步：请回复这条提示，粘贴同一源频道的结束原帖链接。',
    'target': '请回复目标频道的 ID、@用户名或 t.me 链接。',
    'add': '请回复监听频道的 ID、@用户名或 t.me 链接。',
    'pair-add': '请回复两个频道，用逗号分隔，顺序为「源频道,目标频道」。支持 ID、@用户名和 t.me 链接。',
    'pair-edit': '请回复新的目标频道 ID、@用户名或 t.me 链接。',
    'file-download': '请回复要下载的 Telegram 帖子链接，例如 https://t.me/c/1234567890/42。',
}
DANGERS = {
    'reviewclear': (('/queue', 'done', 'all'), '从列表移除全部待核对记录。不会删除频道消息，也不会重新发送。'),
    'logclear': (('/flog', 'clear'), '清空全部本地转发记录。不会删除频道消息或同步映射。'),
    'mapclear': (('clear_mappings',), '清理全部已确认的源帖映射。源帖和目标帖会保留，'
                 '已清理的对应关系将无法用于自动编辑同步或手动同步，已复制的帖子不会因此重发。'),
    'stop': (('/tdlstop',), '停止当前手动转发，清空等待任务和定时重试；已开始的自动同步不会被强停。'),
    'restart': (('/restart',), '重启机器人进程。运行中的任务可能中断，临时配置和面板输入不会保留。'),
}


@dataclass
class Session:
    owner: int
    mid: int = 0
    choices: dict[bytes, str] = field(default_factory=dict)
    history_ids: set | None = None
    result_receipt: bool = False  # Open a fresh home instead of reusing a receipt scheduled for cleanup.
    prompt: object = None
    sync_controls_mid: int = 0
    timer: asyncio.Task | None = None
    stage: str = ''
    start_link: str = ''
    command: tuple[str, ...] = ()
    guard: object = None
    expires: float = 0
    pair_revision: int = 0
    pair_source: int | None = None
    pair_titles: tuple[str, str] = ('', '')
    file_link: str = ''
    file_selection: set[str] = field(default_factory=set)
    file_cleanup: frozenset[str] = frozenset()

    @property
    def prompt_id(self):
        return self.prompt.id if self.prompt else 0


class AdminPanel:
    def __init__(self, bot, *, bot_id, allowed, home, guide, source_id, post_link, export_range):
        self.bot, self.allowed = bot, allowed
        self.bot_id = bot_id
        self.journal = bot.task_queue.journal
        self.home, self.guide = home, guide
        self.webui_url: str | None = None
        self.source_id = source_id
        self.post_link, self.export_range = post_link, export_range
        self.sessions: dict[int, Session] = {}  # At most one current panel and lock per administrator.
        self.locks: dict[int, asyncio.Lock] = {}

    def _lock(self, uid):
        if uid not in self.locks:
            self.locks[uid] = asyncio.Lock()
        return self.locks[uid]

    @staticmethod
    def _keyboard(session, rows):
        token = PREFIX + secrets.token_hex(8).encode() + b':'
        buttons, choices = [], {}
        for row in rows:
            line = []
            for button in row:
                if isinstance(button, types.KeyboardInlineButton):
                    line.append(button)
                    continue
                label, action = button
                data = token + str(len(choices)).encode()
                choices[data] = action
                line.append(Button.inline(label, data))
            buttons.append(line)
        session.choices = choices
        return buttons

    def home_view(self):
        guide_hint = '\n\n命令用法和扫码说明见「📖 使用向导」。'
        if not self.webui_url:
            return self.home + guide_hint, HOME
        hint = ('\n\n🌐 首次使用：点击「🌐 打开 WebUI」获取一次性登录链接，打开后进入「工作台设置」，'
                '按需扫码登录 TDL 转发账号或 Telethon 用户会话。')
        first_row = [('🌐 打开 WebUI', 'webui'), *HOME[0]]
        return self.home + hint + guide_hint, [first_row, *HOME[1:]]

    async def open(self, uid, send, *, guide=False, history=None, mappings=None):
        if not self.allowed(uid):
            return
        async with self._lock(uid):
            if not self.allowed(uid):
                return
            session = Session(uid)
            if mappings is not None:
                text, rows = await self.mappings_view(mappings)
                if not self.allowed(uid):
                    return
            else:
                text, rows = (self.history_view(session, history) if history is not None else
                              (self.guide, [BACK]) if guide else self.home_view())
            session.mid = self.journal.reusable_panel(self.bot_id, uid)
            mode = 'html' if history is not None or mappings is not None else None
            await self._screen(session, text, rows, send=send, parse_mode=mode)

    def history_view(self, s, action):
        rows = self.bot.forward_log.get_all()[::-1]
        if action == 'history' or s.history_ids is None:
            s.history_ids = {str(r['id']) for r in rows}
        else:
            rows = [r for r in rows if str(r['id']) in s.history_ids]
        if action.startswith('hdetail:'):
            _, origin, rid, part = action.split(':', 3)
            index = next((i for i, r in enumerate(rows) if str(r['id']) == rid), None)
            if index is None:
                text, buttons = '此记录不存在、已过期或已清除。', [[('⬅️ 返回记录', f'hpage:{origin}')]]
            else:
                text, buttons = history_detail(rows[index], int(origin) or index // PAGE_SIZE + 1, int(part),
                                               labels=self.bot.history_channel_labels)
        else:
            page = 1 if action == 'history' else int(action.partition(':')[2])
            text, buttons = history_list(rows, page, labels=self.bot.history_channel_labels)
            buttons += [[('🔄 刷新', 'history'), ('🗑 清空本地记录…', 'ask:logclear')]]
        if self.bot.forward_log.write_failed is True:
            text += '\n⚠️ 历史文件写入失败，部分记录仅在内存中。'
        return text, [*buttons, BACK]

    async def mappings_view(self, page):
        result = await self.bot.list_monitor_mappings(page)
        page, pages = result['page'], result['pages']
        lines = ['自动复制的源帖映射', '查看已确认自动复制的源帖及其对应的目标帖。点击链接可打开对应消息。',
                 f"第 {page}/{pages} 页 · 共 {result['total']} 条"]
        for row in result['rows']:
            links = []
            for side, label in (('source', '源帖'), ('target', '目标帖')):
                message = row[f'{side}_message']
                channel = escape(row[f'{side}_label'])
                url = row[f'{side}_url']
                post = f'<a href="{url}">{label} #{message}</a>' if url else f'{label} #{message}'
                links.append(f'{channel} {post}')
            lines.append(' → '.join(links))
        if not result['rows']:
            lines.append('暂无已确认的源帖映射。')
        navigation = []
        if page > 1:
            navigation.append(('上一页', f'mappings:{page - 1}'))
        if page < pages:
            navigation.append(('下一页', f'mappings:{page + 1}'))
        buttons = [navigation] if navigation else []
        if result['total']:
            buttons.append([('🗑 清理全部映射…', 'ask:mapclear')])
        buttons.append([('🔄 刷新', f'mappings:{page}'), *BACK])
        return '\n\n'.join(lines), buttons

    def _end_wait(self, s):
        if s.timer:
            s.timer.cancel()
            s.timer = None
        s.prompt = None

    def _arm_wait(self, s):
        # Start after delivery so network latency does not shorten the input window.
        s.expires = time.monotonic() + TTL
        s.timer = self.bot.schedule_cleanup(partial(self._expire_wait, s), TTL)

    async def _expire_wait(self, s):
        async with self._lock(s.owner):
            if self.sessions.get(s.owner) is not s or s.timer is not asyncio.current_task() or not self.allowed(s.owner):
                return
            s.timer = None
            s.prompt = None
            text = '⌛ 操作已过期，未执行。请返回面板重新开始。'
            try:
                await self._result(s, text)
            except Exception:
                await self._unavailable(s)

    async def _error_notice(self, send, text, **kwargs):
        msg = await send(text, **kwargs)
        self.bot.schedule_panel_receipt(msg, 'error')

    @staticmethod
    def _fit(text):
        # Both the panel and its receipts may contain long runtime lists.
        encoded = text.encode('utf-16-le', errors='replace')
        if len(encoded) > 8192:
            return encoded[:8000].decode('utf-16-le', errors='ignore') + '\n…（列表已截短）'
        return text

    async def _screen(self, s, text, rows, *, replace=False, send=None, parse_mode=None):
        options = dict(buttons=self._keyboard(s, rows), parse_mode=parse_mode, link_preview=False)
        if parse_mode is None:
            text = self._fit(text)
        if s.mid and not replace:
            try:
                await self.bot.client.edit_message(s.owner, s.mid, text, **options)
            except (errors.MessageIdInvalidError, errors.MessageEditTimeExpiredError):
                pass
            else:
                # On open, also finish any cleanup interrupted by a previous shutdown.
                if send is not None:
                    await self._activate(s, s.mid, 'panel', edited=True)
                return
        deliver = send or partial(self.bot.client.send_message, s.owner)
        msg = await deliver(text, **options)
        await self._activate(s, msg.id, 'panel')

    async def _activate(self, s, mid, kind, *, edited=False, controls_mid=0):
        try:
            retired = self.journal.activate_panel(self.bot_id, s.owner, mid, kind, controls_mid=controls_mid)
        except Exception:
            if not edited:
                # The previous pointer is intact. Remove the untracked replacement if possible.
                try:
                    await self.bot.client.delete_messages(s.owner, [mid])
                except Exception:
                    logger.exception('持久化失败后未能撤回新面板')
            raise
        s.mid = 0 if kind == 'prompt' else mid
        s.sync_controls_mid = controls_mid
        s.result_receipt = kind == 'receipt'
        previous = self.sessions.get(s.owner)
        if previous is not s and self.allowed(s.owner):
            if previous:
                self._end_wait(previous)
            self.sessions[s.owner] = s
        for old_mid, old_kind in retired:
            try:
                await self.bot.client.delete_messages(s.owner, [old_mid])
            except Exception as exc:
                logger.warning('未能删除旧面板，尝试移除旧按钮', exc_info=True)
                # Keep a retired input recognizable if someone replies to it later.
                notice = (PROMPT if old_kind in ('prompt', 'sync-controls') else '') + '请使用最新的机器人消息。'
                try:
                    await self.bot.client.edit_message(s.owner, old_mid, notice, buttons=None)
                except errors.MessageIdInvalidError:
                    pass  # The old message is already gone.
                except Exception:
                    continue
                else:
                    if not isinstance(exc, errors.MessageDeleteForbiddenError):
                        continue  # Retry transient deletion failures on the next open or replacement.
            self.journal.forget_retired_panel(self.bot_id, s.owner, old_mid)

    async def callback(self, event):
        if not isinstance(event.data, bytes) or not event.data.startswith(PREFIX):
            return
        uid = event.sender_id
        if uid != event.chat_id or getattr(event, 'via_inline', False) or not self.allowed(uid):
            await event.answer('仅限管理员在与机器人的私聊中操作。', alert=True)
            return
        async with self._lock(uid):
            s = self.sessions.get(uid)
            if not self.allowed(uid):
                return
            prompt_controls = bool(s and s.prompt_id and s.sync_controls_mid
                                   and event.message_id == s.sync_controls_mid)
            if not s or (event.message_id != s.mid and not prompt_controls) or event.data not in s.choices:
                await event.answer('按钮已失效，请使用最新面板或发送 /start。', alert=True)
                if s and event.message_id != s.mid:
                    await self._remove_old_keyboard(event)
                return
            action = s.choices[event.data]
            if prompt_controls:
                await self._exit_prompt(s, event, action)
                return
            if action == 'webui':
                await self._send_webui_link(s, event)
                return
            # A failed navigation can keep its previous keyboard. Execution must never be replayed.
            previous = replace(s, file_selection=set(s.file_selection)) if not s.command and action != 'confirm' else None
            s.choices = {}
            self._end_wait(s)
            try:
                if action == 'confirm':
                    await event.answer('已收到确认，正在核对操作…')
                elif action == 'cancel':
                    await event.answer('已取消，本次操作未执行。')
                elif action in PAGES or action == 'target:refresh':
                    await event.answer('正在读取当前状态…')
                else:
                    await event.answer()
                if not self.allowed(uid):
                    return
                if s.expires and time.monotonic() >= s.expires and action not in ('home', 'cancel'):
                    await self._result(s, '⌛ 操作已过期，未执行。')
                elif action == 'confirm':
                    await self._execute(s, event, s.command, s.guard, partial(self._confirmed_result, s))
                else:
                    s.command, s.stage, s.expires = (), '', 0
                    await self._navigate(s, event, action)
            except Exception:
                if previous and s.mid == previous.mid and self.sessions.get(uid) is s:
                    self.sessions[uid] = previous
                    logger.exception('面板切换失败，保留原面板')
                    try:
                        await self._error_notice(partial(self.bot.client.send_message, uid), '面板切换失败，请重试或发送 /start。')
                    except Exception:
                        logger.warning('未能发送面板切换失败提示', exc_info=True)
                else:
                    await self._unavailable(s)

    async def _exit_prompt(self, s, event, action):
        choices = s.choices
        await event.answer()
        if not self.allowed(s.owner):
            return
        try:
            await self._navigate(s, event, action)
        except Exception:
            if s.mid:
                await self._unavailable(s)
                return
            s.choices = choices
            logger.exception('无法从输入提示返回面板')
            await self._error_notice(event.respond, '面板未能打开，请重试或发送 /start。')
            return
        self._end_wait(s)
        s.stage, s.expires, s.start_link = '', 0, ''

    async def _send_webui_link(self, s, event):
        if not self.webui_url:
            await event.answer('WebUI 尚未配置。', alert=True)
            return
        await event.answer('正在生成一次性登录链接…')
        if not self.allowed(s.owner):
            return
        url = f'{self.webui_url}/#login={SESSIONS.issue_link(s.owner)}'
        try:
            message = await event.respond(
                '🌐 WebUI 登录链接\n\n2 分钟内可使用一次。过期后请从首页重新获取。',
                buttons=[[Button.url('进入 WebUI', url)]], link_preview=False, parse_mode=None,
            )
        except Exception:
            logger.warning('WebUI 登录链接发送失败')
            await self._error_notice(event.respond, '登录链接未能发出，请重新点击「打开 WebUI」。')
            return
        self.bot.schedule_delete(message, LINK_TTL)

    async def _remove_old_keyboard(self, event):
        # Pre-upgrade panels lack a stored message ID. A click identifies the exact message.
        uid, mid = event.sender_id, event.message_id
        if self.journal.is_active_panel(self.bot_id, uid, mid):
            return
        try:
            message = await event.get_message()
            markup = getattr(message, 'reply_markup', None)
            if not getattr(message, 'out', False) or not isinstance(markup, types.ReplyInlineMarkup):
                return
            if not any(isinstance(button, types.KeyboardInlineButton)
                       and isinstance(button.type, types.InlineButtonTypeCallback) and button.type.data == event.data
                       for row in markup.rows for button in row.buttons):
                return
            await self.bot.client.delete_messages(uid, [mid])
        except Exception:
            logger.debug('无法清理已点击的旧面板', exc_info=True)

    async def _unavailable(self, s):
        self.sessions.pop(s.owner, None)
        self._end_wait(s)
        logger.exception('管理面板操作或消息更新失败')
        try:
            await self._error_notice(partial(self.bot.client.send_message, s.owner), '面板未能完成反馈。请用 /start 重开并核对当前状态，不要直接重复操作。')
        except Exception:
            logger.warning('无法投递面板恢复提示，旧面板已失效', exc_info=True)

    async def _navigate(self, s, event, action):
        if action in ('home', 'cancel'):
            s.start_link = ''
            await self._screen(s, *self.home_view(), replace=s.result_receipt)
        elif action == 'help':
            await self._screen(s, self.guide, [BACK])
        elif action == 'system':
            await self._screen(s, '⚙️ 系统管理', [[('🔄 重启机器人…', 'ask:restart')], BACK])
        elif action.startswith('files:'):
            await self._files_action(s, action.split(':')[1:])
        elif action == 'history' or action.startswith(('hpage:', 'hdetail:')):
            text, rows = self.history_view(s, action)
            await self._screen(s, text, rows, parse_mode='html')
        elif action.startswith('mappings:'):
            text, rows = await self.mappings_view(int(action.partition(':')[2]))
            await self._screen(s, text, rows, parse_mode='html')
        elif action.startswith(('pairs:', 'pair:')):
            await self._pairs(s, action)
        elif action in PAGES or action == 'target:refresh':
            page = action.removesuffix(':refresh')
            cmd, options = PAGES[page]
            async def display(text, **kwargs):
                if page in ('monitor', 'target'):
                    text += '\n\n更改备注：/channelname <频道 ID> <备注>'
                refresh = 'target:refresh' if page == 'target' else page
                controls = options[:1] if page == 'monitor' and self.bot.strict_pairs else options
                await self._screen(s, text, [*controls, [('🔄 刷新', refresh), *BACK]], replace=action == 'target')
            await self.bot.run_admin_action(event, [cmd], display)
        elif action.startswith('remove:'):
            ids = self.bot.monitor_chat_ids
            page = min(int(action.split(':')[1]), max(0, (len(ids) - 1) // 8))
            rows = [[(self.bot.channel_label(cid), f'del:{cid}')] for cid in ids[page*8:page*8+8]]
            nav = []
            if page:
                nav.append(('上一页', f'remove:{page-1}'))
            if (page+1)*8 < len(ids):
                nav.append(('下一页', f'remove:{page+1}'))
            await self._screen(s, '请选择要移除的源频道：' if ids else '监听列表为空。',
                               rows + ([nav] if nav else []) + [[('⬅️ 返回监听', 'monitor')]])
        elif action.startswith('del:'):
            cid = action.split(':')[1]
            await self._confirm(s, ('/monitor', 'del', cid), f'从监听列表移除 {self.bot.channel_label(int(cid))}。')
        elif action.startswith('ask:'):
            cmd, warning = DANGERS[action.split(':')[1]]
            await self._confirm(s, cmd, warning)
        elif action.startswith('input:'):
            stage = action.split(':')[1]
            if (stage == 'export' or (stage == 'sync' and not self.bot.strict_pairs)) and self.bot.target_chat_id is None:
                await self._result(s, '❌ 请先在「目标频道」设置接收频道。')
                return
            s.guard = self._snapshot(('/resync',)) if stage == 'sync' else self.bot.target_chat_id if stage == 'export' else None
            await self._prompt(s, stage)

    async def _pairs(self, s, action):
        _, verb, *args = action.split(':')
        state = self.bot.pairing_state()
        back = [('⬅️ 返回配对', 'pairs:1')]
        if action.startswith('pairs:'):
            page = max(1, min(int(verb), (len(state['rows']) + 5) // 6))
            mode = '严格一对一已启用' if state['strict'] else '准备切换 · 自动复制仍使用默认目标频道'
            lines = ['🔀 频道配对', mode]
            if not state['strict'] and state['missing']:
                lines.append(f"还有 {len(state['missing'])} 个现有监听源需要启用配对。")
            if not state['rows']:
                lines.append('尚未配置频道配对。')
            rows = []
            for row in state['rows'][(page - 1) * 6:page * 6]:
                src, dst = int(row['source']), int(row['target'])
                status = '启用' if row['enabled'] else '暂停'
                route = f'{self.bot.channel_label(src, name_limit=16)} → {self.bot.channel_label(dst, name_limit=16)}'
                rows.append([(f'{route} · {status}', f'pair:view:{src}')])
            nav = []
            if page > 1:
                nav.append(('上一页', f'pairs:{page - 1}'))
            if page * 6 < len(state['rows']):
                nav.append(('下一页', f'pairs:{page + 1}'))
            if nav:
                rows.append(nav)
            rows.append([('➕ 新增配对', 'pair:add')])
            if state['strict']:
                rows.append([('关闭严格一对一…', 'pair:deactivate')])
            elif not state['missing'] and any(row['enabled'] for row in state['rows']):
                rows.append([('启用严格一对一', 'pair:activate')])
            s.pair_revision = state['revision']
            await self._screen(s, '\n\n'.join(lines), [*rows, [('⬅️ 返回监听', 'monitor'), *BACK]])
            return

        source = int(args[0]) if args else None
        pair = self.bot.channel_pairs.get(source)
        if verb == 'view' and pair:
            target, enabled, _ = pair
            s.pair_revision = state['revision']
            text = f'🔀 频道配对\n\n{self.bot.channel_label(source)} → {self.bot.channel_label(target)}\n'
            text += '状态：启用' if enabled else '状态：暂停'
            await self._screen(s, text, [[('✏️ 更改目标', f'pair:edit:{source}')],
                                          [('暂停…' if enabled else '恢复…', f'pair:toggle:{source}'),
                                           ('🗑 删除…', f'pair:delete:{source}')], back])
        elif s.pair_revision != state['revision'] or (source is not None and not pair):
            await self._screen(s, '频道配对已改变，请刷新后重试。', [back])
        elif verb in ('add', 'edit'):
            s.pair_source = source
            await self._prompt(s, f'pair-{verb}')
        elif verb == 'activate':
            await self._confirm(s, ('pairs', verb), '启用严格一对一自动复制。手动和批量转发继续使用默认目标，已入队任务保留原目标。')
        elif verb == 'deactivate':
            target = self.bot.monitor_target_channel_id()
            sources = self.bot.monitor_chat_ids
            labels = [self.bot.channel_label(cid) for cid in sources[:20]]
            if len(sources) > len(labels):
                labels.append(f'另有 {len(sources) - len(labels)} 个')
            source_summary = '、'.join(labels) or '未设置'
            detail = ('关闭严格一对一，恢复默认目标频道。\n'
                      f'旧版监听源：{source_summary}\n'
                      f'默认目标频道：{self.bot.channel_label(target) if target else "未设置"}。')
            if not target or not any(self.bot.normalize_source_chat_id(cid) != target for cid in sources):
                detail += '\n当前配置下，关闭后不会自动转发。'
            detail += '\n已保存的配对、源帖映射和已入队任务会保留。'
            await self._confirm(s, ('pairs', verb), detail)
        elif pair and verb in ('toggle', 'delete'):
            label = '暂停' if pair[1] else '恢复'
            if verb == 'delete':
                label = '删除'
            await self._confirm(s, ('pairs', verb, str(source)),
                                f'{label}配对：{self.bot.channel_label(source)} → {self.bot.channel_label(pair[0])}。')

    async def _files_action(self, s, parts):
        staging = getattr(self.bot, 'file_staging', None)
        if staging is None:
            await self._result(s, '文件暂存区尚未配置。请为容器挂载 /downloads。')
            return
        action, *args = parts
        if action == 'downloads':
            await self._prompt(s, 'file-download')
        elif action in ('single', 'album'):
            if not s.file_link:
                await self._result(s, '下载链接已失效，请重新输入。')
                return
            job = staging.enqueue_download(s.owner, s.file_link, album=action == 'album')
            s.file_link = ''
            s.stage = f'file-wait:{job}'
            await self._screen(s, f'📥 下载任务 #{job} 已入队。完成后 Bot 会通知。', [
                [('查看文件任务', 'files:jobs'), ('返回下载暂存', 'files:downloads')], BACK])
        elif action == 'jobs':
            states = {'pending': '等待执行', 'working': '执行中', 'sending': '发送中',
                      'done': '已完成', 'failed': '失败', 'review': '待核对'}
            lines = ['文件任务']
            for job, kind, state, result in staging.recent(s.owner):
                label = '下载' if kind == 'download' else '历史发布'
                lines.append(f'\n#{job} {label} · {states[state]}' + (f'\n{result}' if result else ''))
            await self._screen(s, '\n'.join(lines) if len(lines) > 1 else '暂无文件任务。', [
                [('刷新', 'files:jobs'), ('返回下载暂存', 'files:downloads')], BACK])
        elif action in ('list', 'toggle'):
            if action == 'toggle':
                file_id, page = args
                if file_id in s.file_selection:
                    s.file_selection.remove(file_id)
                elif staging.get(file_id):
                    s.file_selection.add(file_id)
            else:
                page = args[0]
            await self._file_list(s, staging, int(page))
        elif action in ('clean-selected', 'clean-all'):
            eligible = staging.cleanup_candidates(s.file_selection if action == 'clean-selected' else None)
            s.file_cleanup = frozenset(item['id'] for item in eligible)
            if not eligible:
                await self._file_list(s, staging, 0, notice='没有可清理的文件。')
                return
            size = size_label(sum(item['size'] for item in eligible))
            await self._screen(s, f'🧹 确认清理暂存区\n\n将删除 {len(eligible)} 项，预计释放 {size}。'
                               '\n只清理已下载完成的文件。', [
                                   [('确认删除', 'files:delete'), ('取消', 'files:list:0')]])
            s.expires = time.monotonic() + TTL
        elif action == 'delete':
            removed, skipped, failed = staging.clean(s.file_cleanup)
            s.file_cleanup = frozenset()
            s.file_selection.clear()
            size = size_label(sum(item['size'] for item in removed))
            await self._screen(s, f'✅ 已清理 {len(removed)} 项，释放 {size}。\n跳过 {skipped} 项，失败 {failed} 项。', [
                [('查看剩余文件', 'files:list:0')], BACK])
        else:
            await self._result(s, '操作已失效，请返回面板。')

    async def _file_list(self, s, staging, page, *, notice=''):
        files, page, total = staging.files(page=page)
        rows, lines = [], [f'暂存文件列表 · {total} 项']
        if notice:
            lines.append(notice)
        for index, item in enumerate(files, start=1):
            key = item['id']
            lines.append(f'{index}. {item["name"]} · {size_label(item["size"])}')
            label = '☑️' if key in s.file_selection else '⬜'
            rows.append([(f'{label} {index}. {item["name"][:40]}', f'files:toggle:{key}:{page}')])
        nav = []
        if page:
            nav.append(('上一页', f'files:list:{page-1}'))
        if (page + 1) * FILE_PAGE_SIZE < total:
            nav.append(('下一页', f'files:list:{page+1}'))
        if nav:
            rows.append(nav)
        if total:
            rows.append([('删除所选', 'files:clean-selected'), ('🧹 清理全部暂存文件', 'files:clean-all')])
        rows.append([('返回下载暂存', 'files:downloads'), *BACK])
        await self._screen(s, '\n'.join(lines), rows)

    async def file_download_ready(self, owner, job, result):
        async with self._lock(owner):
            s = self.sessions.get(owner)
            if not self.allowed(owner) or not s or s.stage != f'file-wait:{job}':
                return False
            old_choices = s.choices
            try:
                await self._screen(s, f'✅ 文件任务 #{job}\n{result}', [
                    [('暂存文件列表', 'files:list:0'), ('返回下载暂存', 'files:downloads')], BACK])
            except Exception:
                s.choices = old_choices
                logger.warning('未能刷新已完成的下载面板', exc_info=True)
                return False
            s.stage = ''
            return True

    def _snapshot(self, cmd):
        if cmd[0] == 'pairs' or (cmd[0] == '/resync' and self.bot.strict_pairs):
            return ('pairs', self.bot.pairing_revision)
        if cmd[0] in ('/forwardto', '/export_range', '/resync'):
            return self.bot.target_chat_id
        if cmd[0] == '/monitor':
            return tuple(self.bot.monitor_chat_ids)
        return None

    async def _prompt(self, s, stage, *, reply_to=None):
        s.stage = stage
        text = PROMPT + QUESTIONS[stage]
        if stage == 'sync':
            target = ('目标按源频道配对自动选择。' if self.bot.strict_pairs
                      else f'当前目标：{self.bot.channel_label(s.guard)}')
            await self._prompt_with_cancel(s, text, f'🔄 手动同步\n\n只更新已映射的目标帖，不另发。输入 5 分钟内有效。\n{target}')
            return
        if stage == 'file-download':
            await self._prompt_with_cancel(
                s, text, '📥 下载暂存\n\n回复 Telegram 帖子链接，输入 5 分钟内有效。',
                actions=[('查看文件任务', 'files:jobs'), ('暂存文件列表', 'files:list:0')],
            )
            return
        if stage in ('export', 'end'):
            step = '起始链接' if stage == 'export' else '结束链接'
            text += f'\n当前目标：{self.bot.channel_label(s.guard)}'
            await self._prompt_with_cancel(s, text, f'📦 批量转发 · {step}\n\n输入 5 分钟内有效。', reply_to=reply_to)
            return
        text += '\n5 分钟内有效；回复「取消」可退出。'
        prompt = await self.bot.client.send_message(s.owner, text, buttons=Button.force_reply(single_use=True),
                                                    link_preview=False, reply_to=reply_to)
        await self._activate(s, prompt.id, 'prompt')
        s.prompt = prompt
        self._arm_wait(s)

    async def _prompt_with_cancel(self, s, text, description, *, reply_to=None, actions=()):
        client = self.bot.client
        rows = ([actions] if actions else []) + [[('⬅️ 取消并返回', 'home')]]
        controls = await client.send_message(
            s.owner, PROMPT + description,
            buttons=self._keyboard(s, rows), parse_mode=None, link_preview=False,
        )
        try:
            self.journal.stage_panel(self.bot_id, s.owner, controls.id, 'sync-controls')
            # ForceReply must be the last new message so Telegram focuses the reply composer.
            prompt = await client.send_message(s.owner, text, buttons=Button.force_reply(single_use=True),
                                               link_preview=False, reply_to=reply_to)
            await self._activate(s, prompt.id, 'prompt', controls_mid=controls.id)
        except Exception:
            if s.sync_controls_mid != controls.id:
                try:
                    await client.delete_messages(s.owner, [controls.id])
                    self.journal.forget_retired_panel(self.bot_id, s.owner, controls.id)
                except Exception:
                    logger.warning('未能清理未完成的输入按钮', exc_info=True)
            raise
        s.prompt = prompt
        self._arm_wait(s)

    async def remind_if_waiting(self, event):
        """An unthreaded, otherwise ignored text is not consent to run a panel action."""
        uid = event.sender_id
        if uid != event.chat_id or not self.allowed(uid) or getattr(event.message, 'reply_to_msg_id', None):
            return
        s = self.sessions.get(uid)
        if s and s.prompt_id and time.monotonic() < s.expires:
            await self._error_notice(event.respond, '↩️ 这条消息没有回复到输入提示，未执行操作。请对「⌨️ 管理面板输入」那条消息点「回复」，再发送内容。',
                                reply_to=event.message.id)

    async def reply(self, event):
        """Return True for a panel input, including stale replies; never reinterpret one as a post."""
        uid = event.sender_id
        reply_id = getattr(event.message, 'reply_to_msg_id', None)
        if not reply_id or uid != event.chat_id or not uid or uid <= 0:
            return False
        async with self._lock(uid):
            s = self.sessions.get(uid)
            if self.journal.panel_reply_handled(self.bot_id, uid, event.message.id):
                return True
            value = (event.raw_text or '').strip()
            invalid_input = event.message.forward or (event.message.media and not isinstance(
                event.message.media, types.MessageMediaWebPage))
            if not s or not s.prompt_id or reply_id not in (s.prompt_id, s.sync_controls_mid):
                try:
                    original = await event.get_reply_message()
                except Exception:
                    original = None
                missing = original is None or isinstance(original, types.MessageEmpty)
                if not missing and (not original.out or not (original.raw_text or '').startswith(PROMPT)):
                    return False
                if self.allowed(uid):
                    self.journal.remember_panel_reply(self.bot_id, uid, event.message.id)
                    if value != '取消' or invalid_input:
                        text = ('无法识别被回复的消息，原提示可能已失效。未执行或投稿，请重新打开面板或单独发送。'
                                if missing else '此输入提示已失效，未执行或投稿。请使用最新面板。')
                        await self._error_notice(event.respond, text, reply_to=event.message.id)
                return True
            if not self.allowed(uid):
                return True
            self.journal.remember_panel_reply(self.bot_id, uid, event.message.id)
            self._end_wait(s)  # a second reply cannot consume the same prompt
            result = partial(self._input_result, s, event)
            try:
                if time.monotonic() >= s.expires:
                    await result('⌛ 输入已过期，未执行或投稿。')
                elif invalid_input:
                    await result('请回复纯文本或原帖链接，不要转发消息。未执行或投稿。')
                else:
                    await self._submit(s, event, value, result)
            except Exception:
                await self._unavailable(s)
            return True

    def _is_post(self, value):
        if len(value.split()) != 1:
            return False
        src, mid = self.post_link(value)
        return src is not None and self.source_id(str(src)) is not None and mid is not None and 0 < mid < 2**31

    async def _submit(self, s, event, value, result):
        stage = s.stage
        if value == '取消':
            await result('已取消。', receipt_kind='command')
            return
        if stage == 'file-download':
            s.stage, s.expires = '', 0
            try:
                parse_link(value)
            except ValueError as exc:
                await result(f'❌ {exc}')
                return
            s.file_link = value
            await self._screen(s, f'下载来源：{value}\n\n相册链接可下载整组，也可只下载这一项。', [
                [('下载整组', 'files:album'), ('只下载这一项', 'files:single')],
                [('取消', 'files:downloads')]], replace=True)
            s.expires = time.monotonic() + TTL
            return
        if ((stage == 'sync' and self._snapshot(('/resync',)) != s.guard)
                or (stage in ('export', 'end') and self.bot.target_chat_id != s.guard)):
            await result('目标频道已改变，请重新开始。')
            return
        if stage in ('pair-add', 'pair-edit'):
            action = stage.removeprefix('pair-')
            try:
                if action == 'add':
                    values = value.replace('，', ',').split(',')
                    if len(values) != 2:
                        raise ValueError('请用逗号分隔源频道和目标频道。')
                    source, source_title = await self.bot.resolve_channel(values[0])
                    target_value = values[1]
                else:
                    source, source_title, target_value = s.pair_source, '', value
                target, target_title = await self.bot.resolve_channel(target_value)
            except ValueError as exc:
                await result(f'❌ {exc}')
                return
            if s.pair_revision != self.bot.pairing_revision:
                await result('频道配对已改变，请重新开始。')
                return
            source_label = f'{source_title}（{source}）' if source_title else self.bot.channel_label(source)
            target_label = f'{target_title}（{target}）' if target_title else self.bot.channel_label(target)
            s.pair_titles = source_title, target_title
            label = '新增配对' if action == 'add' else '更改配对'
            await self._confirm(s, ('pairs', action, str(source), str(target)),
                                f'{label}：{source_label} → {target_label}。'
                                '\n请核对频道，Bot 尚未验证读写权限。', event=event)
        elif stage == 'sync':
            if self._is_post(value):
                await self._execute(s, event, ('/resync', value), s.guard, result)
            else:
                await result('❌ 链接无效，未加入同步队列。请重新点「手动同步」，回复源频道原帖链接，如 https://t.me/c/1234567890/42。')
        elif stage in ('export', 'end') and self._is_post(value):
            if stage == 'export':
                s.start_link = value
                await self._prompt(s, 'end', reply_to=event.message.id)
            else:
                cmd = ('/export_range', s.start_link, value)
                src, first, last, error = self.export_range(cmd)
                if not error:
                    await self._confirm(s, cmd,
                                        f'源频道：{self.bot.channel_label(src)}\n'
                                        f'目标频道：{self.bot.channel_label(s.guard)}\n消息 ID 范围：{first} 至 {last}',
                                        event=event)
                else:
                    await result('❌ 起止链接必须是同一频道的有效原帖，并按 ID 升序填写。未入队。')
        elif stage in ('target', 'add'):
            try:
                cid, title = await self.bot.resolve_channel(value)
            except ValueError as exc:
                await result(f'❌ {exc}')
                return
            label = f'{title}（{cid}，权限未验证）' if title else self.bot.channel_label(cid)
            if stage == 'target':
                current = self.bot.channel_label(self.bot.target_chat_id) if self.bot.target_chat_id else '未设置'
                await self._confirm(s, ('/forwardto', str(cid)), f'更换目标频道：{current} → {label}', event=event)
            elif title:
                await self._confirm(s, ('/monitor', 'add', str(cid)), f'添加监听：{label}', event=event)
            else:
                await self._execute(s, event, ('/monitor', 'add', str(cid)), self._snapshot(('/monitor',)), result)
        else:
            await result('❌ 输入无效，未执行或投稿。请从面板重新开始。')

    async def _confirm(self, s, cmd, detail, *, event=None):
        s.command, s.guard = cmd, self._snapshot(cmd)
        cancel_label = '⬅️ 取消并返回' if cmd[0] == '/export_range' else '取消'
        text = f'⚠️ 待确认 · 尚未执行\n\n{detail}\n\n请在 5 分钟内选择「确认执行」或「{cancel_label}」。'
        rows = [[('确认执行', 'confirm'), (cancel_label, 'cancel')]]
        if event is not None:
            # Put the actionable card next to the input, not above the ForceReply prompt.
            try:
                card = await event.respond(text, reply_to=event.message.id, buttons=self._keyboard(s, rows),
                                           parse_mode=None, link_preview=False)
            except Exception:
                logger.warning('确认卡片发送失败，尝试更新原面板', exc_info=True)
            else:
                await self._activate(s, card.id, 'panel')
                self._arm_wait(s)
                return
        await self._screen(s, text, rows)
        self._arm_wait(s)

    async def _result(self, s, text, **kwargs):
        s.command, s.stage, s.expires = (), '', 0
        await self._screen(s, text, [BACK])

    async def _confirmed_result(self, s, text, **kwargs):
        # Update the clicked card. If it disappeared, deliver the actual result, not a retry.
        try:
            await self._result(s, text)
        except Exception:
            logger.warning('确认结果未能写入面板，改发新面板', exc_info=True)
            await self._screen(s, text, [BACK], replace=True)

    async def _input_result(self, s, event, text, *, link_preview=False, receipt_kind='error'):
        try:
            receipt = await event.respond(self._fit(text), reply_to=event.message.id,
                                          buttons=self._keyboard(s, [BACK]), parse_mode=None, link_preview=link_preview)
        except Exception:
            logger.warning('输入回执发送失败，尝试单独发送结果', exc_info=True)
            await self._result(s, text)
            return
        s.command, s.stage, s.expires = (), '', 0
        await self._activate(s, receipt.id, 'receipt')
        self.bot.schedule_panel_receipt(receipt, receipt_kind)

    async def _execute(self, s, event, cmd, guard, result):
        if not cmd or not self.allowed(s.owner) or self._snapshot(cmd) != guard:
            await result('配置已改变或操作已失效。未执行，请重新开始。')
            return
        s.command, s.expires = (), 0
        if cmd[0] == 'pairs':
            source_title, target_title = s.pair_titles
            s.pair_titles = ('', '')
            try:
                self.bot.change_channel_pair(cmd[1], *(int(cid) for cid in cmd[2:]), expected=guard[1],
                                             source_title=source_title, target_title=target_title)
            except ValueError as exc:
                await result(f'❌ {exc}')
            else:
                await result('✅ 频道配对已更新。')
            return
        if cmd == ('clear_mappings',):
            try:
                removed = await self.bot.clear_monitor_mappings()
            except ValueError as exc:
                await result(str(exc))
            else:
                await result(f'✅ 已清理 {removed} 条源帖映射。')
            return
        if cmd[:2] == ('/monitor', 'add'):
            result = partial(result, receipt_kind='command')
        await self.bot.run_admin_action(event, list(cmd), result, panel_feedback=True)
