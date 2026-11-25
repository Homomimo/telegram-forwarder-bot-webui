"""Per-operation evidence and Telegram history views (independent of edit mappings)."""
from dataclasses import dataclass
from html import escape
from telethon import types, utils

# Member result: positive = receipted target ID, 0 = uncertain, -1 = definitely not sent.
UNKNOWN, FAILED = 0, -1
PAGE_SIZE = 5
MANUAL_TDL_ROUTE = '手动转发（TDL 账号发送） · 无逐条成功回执'
# Old automatic records do not identify the publisher or whether media was downloaded.
_LEGACY_ROUTES = {'TDL 回退上传': '重新发布（旧记录未区分媒体和发布账号）',
                  'TDL CLI（无逐条成功回执）': MANUAL_TDL_ROUTE,
                  'Bot 回执': 'Bot 发布', '用户账号回执': '用户账号发布'}


@dataclass
class Delivery:
    kind: str
    route: str
    members: dict[int, int] | None = None  # None: source count could not be established
    album: bool = False  # The destination is an album, not just the source.
    source_channel: int | None = None  # Keep the actual peer even if a public username later changes.

    job_id: int | None = None
    album_groups: list[list[int]] | None = None

    def fields(self):
        result = dict(source_channel=self.source_channel, kind=self.kind, route=self.route, album=self.album,
                    members=None if self.members is None else list(self.members.items()))
        if self.album_groups:
            result['album_groups'] = self.album_groups
        if self.job_id is not None:
            result.update(job_id=self.job_id, origin="monitor")
        return result


def post_url(channel, mid, album=False):
    if type(channel) is not int or type(mid) is not int or not 0 < mid < 2**31:
        return None
    if channel < 0:
        channel, peer = utils.resolve_id(channel)
        if peer is not types.PeerChannel:
            return None
    if 0 < channel <= 997_852_516_352:
        return f'https://t.me/c/{channel}/{mid}' + ('?single' if album else '')


def post_anchor(row, mid):
    url = post_url(row['to'], mid, row.get('album'))
    return f'<a href="{url}">查看目标消息 {mid}</a>' if url else None


def counts(row):
    if 'members' not in row:
        return '条数：旧版未记录'
    if row['members'] is None:
        return '条数未知（未取得完整源消息清单）'
    results = [result for _, result in row['members']]
    return (f'涉及 {len(results)} · 确认送达 {sum(r > 0 for r in results)} · '
            f'明确失败 {results.count(FAILED)} · 待核对 {results.count(UNKNOWN)}')


def _target(row):
    member = next((r for _, r in row.get('members') or () if r > 0), None)
    mid = member or (row.get('target_message_id') if row['success'] else None)
    return post_anchor(row, mid) or '未取得目标消息 ID，暂无链接'


def _heading(row):
    icon = '✅' if row['success'] else '⚠️' if row.get('unconfirmed') else '❌'
    return f'{icon} #{escape(str(row["id"]))}　{escape(row.get("kind", "旧版记录"))}'


def history_list(rows, page):
    """rows are newest first, bounded by the caller's snapshot."""
    total = len(rows)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(pages, page))
    text = [f'📝 转发记录 · 第 {page}/{pages} 页 · 共 {total} 次操作']
    buttons = []
    for row in rows[(page-1)*PAGE_SIZE:page*PAGE_SIZE]:
        source = row['from']
        preview = escape(source[:100]) + ('…' if len(source) > 100 else '')
        text.append(f'{_heading(row)}\n来源：{preview}\n{escape(row["date"][:32])} · {counts(row)}\n目标帖：{_target(row)}')
        buttons.append((f'#{row["id"]} 详情', f'hdetail:{page}:{row["id"]}:1'))
    if not total:
        text.append('暂无转发记录')
    nav = ([] if page == 1 else [('上一页', f'hpage:{page-1}')]) + (
        [] if page == pages else [('下一页', f'hpage:{page+1}')])
    keys = [buttons[i:i+3] for i in range(0, len(buttons), 3)]
    return '\n\n'.join(text), keys + ([nav] if nav else [])


def _chunks(text, size=1200):
    # Split untrusted strings before escaping them; never split HTML entities or surrogate pairs.
    for i in range(0, len(text), size):
        yield escape(text[i:i+size])


def history_detail(row, origin, page):
    # Bound the visible UTF-16 size, not the number of members; links always stay intact.
    chunks, lines, used = [], [], 0
    def line(html):
        nonlocal lines, used
        cost = len(html.encode('utf-16-le')) // 2 + 1
        if lines and used + cost > 3100:
            chunks.append('\n'.join(lines)); lines, used = [], 0
        lines.append(html); used += cost
    for field in ('来源：' + row['from'], '时间：' + row['date']):
        for part in _chunks(field, 500):
            line(part)
    route = row.get('route', '旧版未记录')
    line('发送方式：' + escape(_LEGACY_ROUTES.get(route, route)))
    if type(row.get('source_channel')) is int:
        line(f'源频道 ID：{row["source_channel"]}')
    line(f'目标频道 ID：{row["to"]}')
    members = row.get('members')
    if not members or not any(result > 0 for _, result in members):
        line('目标帖：' + _target(row))
    for mid, result in members or ():
        target = post_anchor(row, result) if result > 0 else None
        line(f'源消息 {mid} → ' + (target or ('明确失败／未发送' if result == FAILED else '待核对，无目标链接')))
    if row.get('error'):
        line('诊断：')
        for part in _chunks(row['error'], 500):
            line(part)
    chunks.append('\n'.join(lines))
    page = min(len(chunks), max(1, page))
    root = f'hdetail:{origin}:{row["id"]}:'
    nav = ([] if page == 1 else [('上一页', root + str(page-1))]) + (
        [] if page == len(chunks) else [('下一页', root + str(page+1))])
    text = f'{_heading(row)} · 详情 {page}/{len(chunks)}\n{counts(row)}\n\n{chunks[page-1]}'
    return text, ([nav] if nav else []) + [[('⬅️ 返回记录', f'hpage:{origin}')]]
