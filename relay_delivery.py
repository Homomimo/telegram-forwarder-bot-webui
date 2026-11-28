"""Album-aware publication used by both front ends in the integrated deployment."""

import json
import secrets
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from telethon import errors, functions

import accounts
from clone_receipts import prepare_clone, read_receipts, Uncertain
from forward_records import FAILED, UNKNOWN
from monitor_clone import receipt_pairs


def album_batches(rows, first, last, *, is_range, single, low, high):
    by_id = {}
    groups = defaultdict(list)
    for row in rows:
        raw = row.get("raw") or {}
        mid, group = row.get("id"), raw.get("GroupedID")
        if (
            row.get("type") != "message"
            or type(mid) is not int
            or raw.get("ID") != mid
            or type(group) is not int
            or not low <= mid <= high
            or mid in by_id
        ):
            raise ValueError("导出缺少可靠的消息或相册信息，未发送")
        by_id[mid] = group
        if group:
            groups[group].append(mid)
    selected = sorted(mid for mid in by_id if first <= mid <= last)
    if not selected:
        raise ValueError("未找到指定源消息，未发送")
    batches, seen = [], set()
    for mid in selected:
        group = by_id[mid]
        if single or not group:
            batches.append([mid])
        elif group not in seen:
            members = sorted(groups[group])
            if (
                not 2 <= len(members) <= 10
                or (members[0] == low and low > 1)
                or (members[-1] == high and high < 2**31 - 1)
            ):
                raise ValueError("无法确认完整相册，未拆分发送")
            if is_range and not first <= members[0] <= members[-1] <= last:
                raise ValueError(
                    f"范围截断相册 {members[0]}–{members[-1]}，整次任务未发送"
                )
            batches.append(members)
            seen.add(group)
    return batches


async def plan(bot, task, export):
    is_range = task.mode == "export_range"
    if is_range:
        channel = int(bot.tdl.normalize_chat_id(task.source_chat_id))
        first, last = task.message_ids
        if last - first >= 500:
            raise ValueError("每次范围最多 500 个 ID，请分段并避开相册边界")
        source = str(channel)
    else:
        channel, first = bot.tdl.parse_message_link(task.link)
        last = first
        source = (
            str(channel) if channel else "@" + urlsplit(task.link).path.split("/")[1]
        )
    low, high = max(1, first - 100), min(2**31 - 1, last + 100)
    if not accounts.summary(bot.console_data)["tdl"]:
        raise ValueError("请先在网页设置中扫码登录 TDL 账号")
    code, _, _ = await bot.tdl._run_command(
        [
            bot.tdl.tdl_path,
            "chat",
            "export",
            "-c",
            source,
            "-T",
            "id",
            "--all",
            "--raw",
            "-i",
            f"{low},{high}",
            "-o",
            export,
        ]
    )
    if code:
        raise ValueError("无法读取源频道，请检查 TDL 授权与频道权限；未发送")
    data = json.loads(Path(export).read_text("utf-8-sig"))
    resolved = data.get("id")
    if type(resolved) is not int or resolved <= 0 or (channel and channel != resolved):
        raise ValueError("导出频道与任务不符，未发送")
    if str(resolved) == bot.tdl.normalize_chat_id(task.target_chat_id):
        raise ValueError("源和目标频道相同，未发送")
    batches = album_batches(
        data["messages"],
        first,
        last,
        is_range=is_range,
        single=not is_range and task.link.endswith("?single"),
        low=low,
        high=high,
    )
    return resolved, batches


async def clone(bot, task, channel, ids, target=None, actor=None, *, progress=None):
    request = SimpleNamespace(
        channel=channel, ids=ids, grouped=len(ids) > 1, target=target, actor=actor
    )
    dest = bot.tdl.normalize_chat_id(task.target_chat_id)
    args, receipt, token = prepare_clone(
        bot.console_data, bot.tdl.tdl_path, {"id": task.job_id, "dest": dest}, request
    )
    if progress is not None:
        code, _, _ = await bot.tdl._run_command([*args, "--disable-progress-ps"], progress)
    else:
        code, _, _ = await bot.tdl._run_command(args)
    if code:
        raise Uncertain("TDL clone 未获完整回执，请核对目标频道；不自动重发")
    return read_receipts(receipt, token, channel, dest, ids, target, actor)


async def publish(bot, task, route, channel, batches, mark, delivery, *, progress=None):
    total = sum(map(len, batches))
    confirmed = 0
    for index, ids in enumerate(batches, start=1):
        if route:
            client, source, target, _ = route
            nonces = {mid: secrets.randbits(63) or 1 for mid in ids}
            request = functions.messages.ForwardMessagesRequest(
                from_peer=source,
                to_peer=target,
                id=ids,
                random_id=list(nonces.values()),
                drop_author=True,
            )
            mark(dict.fromkeys(ids, UNKNOWN))
            try:
                result = await client(request)
            except errors.ChatForwardsRestrictedError:
                mark(
                    dict.fromkeys(ids, FAILED)
                )  # Explicit rejection: safe to switch this batch only.
            except (
                errors.FloodWaitError,
                errors.ChatWriteForbiddenError,
                errors.ChatAdminRequiredError,
                errors.ChannelPrivateError,
                errors.MessageIdInvalidError,
            ):
                mark(dict.fromkeys(ids, FAILED))
                raise
            else:
                pairs = receipt_pairs(result, nonces, allow_short=False)
                mark(pairs)
                if set(pairs) != set(ids):
                    raise Uncertain(
                        "原生转发回执不完整，已停止后续任务；不会自动换通道重发"
                    )
                confirmed += len(pairs)
                continue
        delivery.route = "原生转发 / TDL clone（禁转回退）" if route else "TDL clone"
        task.manual_meta["route"] = delivery.route
        bot.task_queue.journal.save(task, "sending")
        options = {}
        if progress is not None:
            async def report(record=None):
                await progress(confirmed, total, index, len(batches), record)
            await report()
            options["progress"] = report
        mark(dict.fromkeys(ids, UNKNOWN))
        pairs = await clone(bot, task, channel, ids, **options)
        mark(pairs)
        confirmed += len(pairs)
