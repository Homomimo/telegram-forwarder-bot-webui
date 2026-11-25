"""Bounded, read-only rendering of the Telegram task queue."""

MODE_LABELS = {'clone': '手动转发', 'export_range': '批量转发',
               'monitor_copy': '自动复制', 'monitor_edit': '自动编辑'}
QUEUE_TEXT_LIMIT = 3800  # UTF-16 code units; leave headroom below Telegram's limit.


def queue_excerpt(text, limit=80):
    text = ' '.join(text.splitlines())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def format_duration(seconds):
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f'{hours}小时{minutes}分'
    if minutes:
        return f'{minutes}分{seconds}秒'
    return f'{seconds} 秒'


def _task_label(task):
    mode = task['mode']
    label = f"#{task['job_id']} {MODE_LABELS.get(mode, queue_excerpt(mode, 24))}"
    if task['range']:
        first, last = task['range']
        label += f' {first}–{last}'
    elif mode == 'monitor_copy' and task['count'] and task['count'] > 1:
        label += f" {task['count']} 条"
    return label


def _task_row(category, task, index):
    text = _task_label(task)
    source = queue_excerpt(task['link'])
    target = task['target_chat_id']
    if category == 'queued':
        return f"{index}. {text} · {source} → {target} · 已等 {format_duration(task['age'])}"
    if category == 'retry':
        attempts = []
        if task['retries']:
            attempts.append(f"第 {task['retries']} 次")
        if task['store_retries']:
            attempts.append(f"映射库第 {task['store_retries']} 次")
        return ' · '.join([text, *attempts, f"{task['ready_in']} 秒后", f'{source} → {target}'])
    if category == 'albums':
        return f"#{task['job_id']} 已收 {task['count']} 条 · {source} → {target}"
    if category == 'peer':
        reason = '等待相册补齐' if task['waiting_for_album'] else '等待频道实体'
        text += f' · {reason}'
    return f'{text} · {source} → {target}'


def render_queue_status(snapshot, review_text):
    counts, current = snapshot['counts'], snapshot['current_task']
    state = '正在停止' if snapshot['stopping'] else '运行中' if snapshot['running'] else '已停止'
    breakdown = [f'{label} {counts[key]}' for key, label in (
        ('queued', '排队'), ('retry', '定时重试'), ('albums', '收集相册'),
        ('peer', '等待频道 / 相册'), ('store', '等待映射库'), ('early_edits', '待映射编辑'),
    )]
    if counts['followup']:
        breakdown.append(f"后续编辑 {counts['followup']}")
    heading = [f"📋 转发队列状态\n运行状态：{state}\n共 {sum(counts.values())} 项：" + ' · '.join(breakdown)]
    if current:
        count = '尚未确认' if current['count'] is None else f"{current['count']} 条"
        lines = [f"▶️ 正在处理 {_task_label(current)} · 已运行 {format_duration(current['elapsed'])}",
                 f"源：{queue_excerpt(current['link'])}", f"目标：{current['target_chat_id']} · 消息数：{count}"]
        if current['followup']:
            lines.append('另有 1 次后续编辑已合并，将读取源帖最新内容。')
        heading.append('\n'.join(lines))

    sections = []
    for key, title in (
        ('queued', '⏳ 排队中'), ('retry', '🔁 定时重试'), ('albums', '📸 收集相册'),
        ('peer', '🕓 等待频道实体 / 相册补齐（等相关源更新，不会自行出队；可用 /tdlstop 清除）'),
        ('store', '🔒 等待映射库解锁（不会自动出队；解锁后重启可恢复，也可用 /tdlstop 清除）'),
    ):
        if counts[key]:
            rows = [_task_row(key, t, i) for i, t in enumerate(snapshot[key], 1)]
            sections.append((key, title, counts[key], rows))
    footer = []
    if any(task['waiting_for_album'] for task in snapshot['peer']):
        footer.append('源相册已发完但目标未收到？核对实际项数后，用 /queue album <任务ID> <项数> 重新核对并入队。')
    if counts['early_edits']:
        footer.append(f"✏️ 待映射编辑：{counts['early_edits']} 项（等源帖建立映射，不会自行出队；可用 /tdlstop 清除）")
    if review_text:
        footer.append(review_text)

    def render():
        blocks = []
        for key, title, total, rows in sections:
            if key == 'queued':
                title += f'（按执行顺序，显示前 {len(rows)} 项）' if rows else '（按执行顺序）'
            omitted = [f'…还有 {total - len(rows)} 项'] if total > len(rows) else []
            blocks.append('\n'.join([title + '：', *rows, *omitted]))
        return '\n\n'.join([*heading, *blocks, *footer])

    text = render()
    # Trim whole rows, starting with the FIFO, never cutting the review page or its cursor.
    for minimum in (1, 0):
        for _, _, _, rows in sections:
            while len(rows) > minimum and len(text.encode('utf-16-le')) // 2 > QUEUE_TEXT_LIMIT:
                rows.pop()
                text = render()
    return text
