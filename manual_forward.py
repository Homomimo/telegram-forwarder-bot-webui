"""Manual publication. The selected account and its input peers stay together for the operation."""
import json
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from telethon import errors, functions, types, utils

from forward_records import FAILED, UNKNOWN
from monitor_clone import media_key, publish_request, read_members, receipt_pairs, upload_batch


class MissingReceipt(Exception):
    """The RPC completed without evidence for every submitted random ID."""


def read_export(path, channel, first, last):
    """Validate both peer and strict bounds before either publisher sees the file."""
    path = Path(path)
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if (not isinstance(data, dict) or type(data.get('id')) is not int or data['id'] != channel
            or not isinstance(data.get('messages'), list)):
        raise ValueError('TDL 导出的源频道或消息列表不匹配，未发送')
    rows = {}
    for row in data['messages']:
        mid = row.get('id') if isinstance(row, dict) else None
        if type(mid) is not int or not first <= mid <= last:
            raise ValueError('TDL 导出含无效或超出范围的消息 ID，未发送')
        rows.setdefault(mid, row)
    if len(rows) != len(data['messages']):
        data['messages'] = list(rows.values())
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return sorted(rows)


async def select_route(bot, task, source_id, first):
    """Only this read-only preflight may fall back to a different publishing account."""
    if first is None:
        return None
    clients = [bot.client] + ([bot.user_client] if bot.is_user_monitor_active() else [])
    for client in clients:
        try:
            source = (await bot.get_monitor_channel_peer(client, source_id) if source_id else
                      await client.get_input_entity(urlsplit(task.link).path.split('/')[1]))
            if not isinstance(source, types.InputPeerChannel) or (source_id and source.channel_id != source_id):
                continue
            target_id = utils.resolve_id(task.target_chat_id)[0]
            target = await bot.get_monitor_channel_peer(client, target_id)
            if not isinstance(target, types.InputPeerChannel) or target.channel_id != target_id:
                continue
            entity = await client.get_entity(target)
            if not isinstance(entity, types.Channel) or entity.id != target_id or entity.min or entity.left:
                continue
            if entity.broadcast and not (entity.creator or getattr(entity.admin_rights, 'post_messages', False)):
                continue
            if not entity.creator and not entity.admin_rights and any(
                    getattr(rights, 'send_messages', False) for rights in (entity.banned_rights, entity.default_banned_rights)):
                continue
            post = await client.get_messages(source, ids=first)
            if isinstance(post, types.Message) and post.id == first and post.peer_id == types.PeerChannel(source.channel_id):
                return client, source, target, post
        except (errors.ChannelPrivateError, errors.ChatAdminRequiredError, errors.PeerIdInvalidError,
                errors.ChannelInvalidError, errors.BotMethodInvalidError, errors.UsernameNotOccupiedError,
                errors.UsernameInvalidError, ValueError):
            continue
    return None


async def publish(bot, route, ids, group, mark):
    client, source, target, _ = route
    # For ranges, every source ID is a separate request, never expand beyond the export.
    for batch in ([ids] if group is not None else ([mid] for mid in ids)):
        reserved = {mid: (secrets.randbits(63) or 1) for mid in batch}
        request = functions.messages.ForwardMessagesRequest(from_peer=source, to_peer=target,
            id=batch, random_id=list(reserved.values()), drop_author=True)
        mark(dict.fromkeys(batch, UNKNOWN))
        try:
            try:
                sent = await client(request)
            except errors.ChatForwardsRestrictedError:
                # An explicit rejection, not a lost response. UploadMedia itself does not publish.
                mark(dict.fromkeys(batch, FAILED))
                with tempfile.TemporaryDirectory(prefix='manual-tdl-') as directory:
                    async def read():
                        if group is not None:
                            posts = await read_members(client, source, batch, group)
                        else:
                            posts = await client.get_messages(source, ids=batch)
                        if [getattr(p, 'id', None) for p in posts] != batch or any(not isinstance(p, types.Message) for p in posts):
                            raise ValueError('源消息或相册成员已变化，未发布')
                        for p in posts:
                            if media_key(p) == 'text' and (group is not None or not p.raw_text):
                                raise ValueError('缺少可发送的源内容')
                        return posts
                    posts = await read()
                    media = await upload_batch(bot.tdl, client, source, target, source.channel_id, posts, directory)
                    latest = await read()
                    if list(map(media_key, latest)) != list(map(media_key, posts)):
                        raise ValueError('下载期间源媒体有变化，未发布')
                    request = publish_request(target, latest, media, reserved)
                    mark(dict.fromkeys(batch, UNKNOWN))
                    sent = await client(request)
            pairs = receipt_pairs(sent, reserved, allow_short=not isinstance(request, functions.messages.ForwardMessagesRequest))
        except (errors.ChatWriteForbiddenError, errors.ChatAdminRequiredError, errors.ChannelPrivateError,
                errors.MessageIdInvalidError, errors.FloodWaitError, errors.ChatForwardsRestrictedError):
            mark(dict.fromkeys(batch, FAILED))
            raise
        mark(pairs)
        if len(pairs) != len(batch):
            raise MissingReceipt('缺少逐条回执；已停止后续发送，不会换通道重发')
