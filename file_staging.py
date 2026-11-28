"""Persistent downloads and explicitly confirmed Bot publications."""

import asyncio
from contextlib import closing
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time

from telethon import errors, functions, types, utils

import accounts
from channel_input import numeric_channel, parse_reference
from forward_records import post_url
from monitor_clone import expected_size, media_key, receipt_pairs

logger = logging.getLogger(__name__)
DOWNLOADS = Path('/downloads')
PAGE_SIZE = 7
PREVIEW_PAGE_SIZE = 24
MAX_ALBUM = 10
DRAFT_TTL = 1800
PREVIEW_TYPES = frozenset({
    'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/avif',
    'video/mp4', 'video/webm', 'video/ogg', 'video/quicktime',
})
FILE_ID = re.compile(r'[1-9]\d{0,18}-[1-9]\d{0,9}')


def mounted_downloads():
    if not DOWNLOADS.is_dir() or DOWNLOADS.is_symlink():
        return None
    # A Linux bind mount may have the same device number as its parent.
    try:
        with open('/proc/self/mountinfo', encoding='utf-8') as mounts:
            if any(line.split()[4] == str(DOWNLOADS) for line in mounts):
                return DOWNLOADS
    except FileNotFoundError:
        pass
    return None


def parse_link(link):
    reference = parse_reference(link)
    if reference.post is None:
        raise ValueError('请发送单条 Telegram 帖子链接，例如 https://t.me/c/1234567890/42。')
    return reference.peer, reference.post


def media_info(post):
    if getattr(post, 'noforwards', False):
        raise ValueError('该消息已启用内容保护，无法暂存或重新发布。')
    key = media_key(post)
    if key.startswith('photo:'):
        return {'name': f'照片-{post.id}.jpg', 'kind': 'photo', 'mime': 'image/jpeg'}
    if not key.startswith('document:'):
        raise ValueError('该消息没有可用的图片、视频或文件。')
    doc = post.media.document
    video = next((attr for attr in doc.attributes if isinstance(attr, types.DocumentAttributeVideo)), None)
    name = next((attr.file_name for attr in doc.attributes if isinstance(attr, types.DocumentAttributeFilename)), '')
    name = name.replace('\\', '/').rsplit('/', 1)[-1]
    name = ''.join(c for c in name if c.isprintable()).strip()[:160] or f'文件-{post.id}'
    info = {'name': name, 'kind': 'video' if video and not video.round_message else 'file', 'mime': doc.mime_type}
    if info['kind'] == 'video':
        info['video'] = [video.duration, video.w, video.h]
    return info


def size_label(size):
    if size < 1024:
        return f'{size} B'
    if size < 1024**2:
        return f'{size / 1024:.1f} KB'
    return f'{size / 1024**2:.1f} MB'


@dataclass
class Draft:
    target: int | None
    items: list = field(default_factory=list)
    caption: str = ''
    target_title: str = ''
    updated: float = field(default_factory=time.monotonic)

    def add(self, items):
        known = {(item['source'], item['ref']) for item in self.items}
        added = []
        for item in items:
            key = item['source'], item['ref']
            if key not in known:
                known.add(key)
                added.append(item)
        combined = [*self.items, *added]
        if len(combined) > MAX_ALBUM:
            raise ValueError('相册最多 10 项，请先移除部分文件。')
        if len(combined) > 1 and any(item['kind'] == 'file' for item in combined):
            raise ValueError('普通文件请单独发布。相册支持图片和视频。')
        self.items = combined
        return len(added)


class FileStaging:
    def __init__(self, bot, data, root):
        self.bot = bot
        self.root = root
        self.db_path = data / 'file_staging.sqlite3'
        self.drafts: dict[int, Draft] = {}
        self.wake = asyncio.Event()
        self.worker = None
        # Keep the database on /data and the media under the independently mounted directory.
        with closing(self.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY, job INTEGER NOT NULL, '
                       'payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS files_by_job ON files(job DESC, id)')
            db.execute('CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER NOT NULL, '
                       'kind TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, result TEXT NOT NULL DEFAULT \'\', '
                       'ready REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0)')

    def connect(self):
        return sqlite3.connect(self.db_path)

    def start(self):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE jobs SET state='review', result='容器在发送期间重启，请先核对目标频道。' WHERE state='sending'")
            db.execute("UPDATE jobs SET state='pending' WHERE state='working'")
        self.worker = asyncio.create_task(self.run())

    async def stop(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None

    def draft(self, owner, *, create=False):
        now = time.monotonic()
        for user in tuple(self.drafts):
            if now - self.drafts[user].updated >= DRAFT_TTL:
                del self.drafts[user]
        if create and owner not in self.drafts:
            self.drafts[owner] = Draft(self.bot.target_chat_id)
        if draft := self.drafts.get(owner):
            draft.updated = now
        return draft

    def enqueue(self, owner, kind, payload):
        with closing(self.connect()) as db, db:
            job = db.execute('INSERT INTO jobs(owner,kind,state,payload) VALUES(?,?,?,?)',
                             (owner, kind, 'pending', json.dumps(payload))).lastrowid
        self.wake.set()
        return job

    def recent(self, owner):
        with closing(self.connect()) as db:
            rows = db.execute('SELECT id,kind,state,result FROM jobs WHERE owner=? ORDER BY id DESC LIMIT 5', (owner,))
            return list(rows)

    def downloaded(self, owner, job=None):
        with closing(self.connect()) as db:
            if job is None:
                row = db.execute("SELECT id FROM jobs WHERE owner=? AND kind='download' AND state='done' "
                                 'ORDER BY id DESC LIMIT 1', (owner,)).fetchone()
                if row is None:
                    return []
                job = row[0]
            rows = db.execute('SELECT files.id,files.payload FROM files JOIN jobs ON jobs.id=files.job '
                              "WHERE jobs.id=? AND jobs.owner=? AND jobs.kind='download' AND jobs.state='done' "
                              "ORDER BY CAST(substr(files.id,instr(files.id,'-')+1) AS INTEGER)", (job, owner))
            return self.decode_files(rows)

    @staticmethod
    def decode_files(rows):
        return [dict(json.loads(payload), id=file_id) for file_id, payload in rows]

    def files(self, *, preview=False, page=0):
        with closing(self.connect()) as db:
            size = PREVIEW_PAGE_SIZE if preview else PAGE_SIZE
            clause, params = '', ()
            if preview:
                params = tuple(sorted(PREVIEW_TYPES))
                clause = " WHERE json_extract(payload,'$.mime') IN (" + ','.join('?' for _ in params) + ')'
            total = db.execute('SELECT count(*) FROM files' + clause, params).fetchone()[0]
            page = min(max(0, page), max(0, (total - 1) // size))
            rows = db.execute('SELECT id,payload FROM files' + clause + ' ORDER BY job DESC,id LIMIT ? OFFSET ?',
                              (*params, size, size * page))
            return self.decode_files(rows), page, total

    def get(self, file_id):
        if not FILE_ID.fullmatch(file_id):
            return None
        with closing(self.connect()) as db:
            return next(iter(self.decode_files(db.execute('SELECT id,payload FROM files WHERE id=?', (file_id,)))), None)

    def open_media(self, item):
        # Refuse symlinks and nonregular files even if the mounted directory was edited on the host.
        fd = os.open(self.root / f'relay-{item["id"]}', os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
                     | getattr(os, 'O_NONBLOCK', 0))
        try:
            status = os.fstat(fd)
            if not stat.S_ISREG(status.st_mode) or status.st_size != item['size']:
                raise OSError('暂存文件已变化')
            return os.fdopen(fd, 'rb')
        except BaseException:
            os.close(fd)
            raise

    def cleanup_candidates(self, ids=None):
        now = time.monotonic()
        keep = {item['ref'] for draft in self.drafts.values() if now - draft.updated < DRAFT_TTL
                for item in draft.items if item['source'] == 'staging'}
        with closing(self.connect()) as db:
            jobs = db.execute("SELECT payload FROM jobs WHERE kind='publish' AND state IN ('pending','working','sending')")
            for (payload,) in jobs:
                keep.update(item['ref'] for item in json.loads(payload)['items'] if item['source'] == 'staging')
            rows = db.execute('SELECT id,payload FROM files ORDER BY job DESC,id')
            candidates = [item for item in self.decode_files(rows) if ids is None or item['id'] in ids]
            eligible = [item for item in candidates if item['id'] not in keep]
            return eligible, len(candidates) - len(eligible)

    def clean(self, ids):
        removed, failed = [], 0
        with closing(self.connect()) as db, db:
            eligible, _ = self.cleanup_candidates(ids)
            for item in eligible:
                try:
                    (self.root / f'relay-{item["id"]}').unlink(missing_ok=True)
                except OSError:
                    failed += 1
                    continue
                db.execute('DELETE FROM files WHERE id=?', (item['id'],))
                removed.append(item)
        return removed, len(ids) - len(removed) - failed, failed

    async def source(self, link):
        source, mid = parse_link(link)
        clients = [self.bot.client]
        if self.bot.is_user_monitor_active():
            clients.append(self.bot.user_client)
        for client in clients:
            try:
                peer = (await self.bot.get_monitor_channel_peer(client, source) if isinstance(source, int)
                        else await client.get_input_entity(source))
                if not isinstance(peer, types.InputPeerChannel):
                    raise ValueError('只支持频道帖子。')
                post = await client.get_messages(peer, ids=mid)
                if post:
                    return client, peer, post
            except (ValueError, errors.ChannelPrivateError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError):
                continue
        raise ValueError('无法读取该帖子。请核对链接，并确认 Bot 或 Telethon 用户会话能访问源频道。')

    async def download(self, job, payload):
        # A hard stop may leave unpublished files for this still-pending job.
        for pattern in (f'.relay-{job}-*.part', f'relay-{job}-*'):
            for orphan in self.root.glob(pattern):
                file_id = orphan.name.removeprefix('.').removeprefix('relay-').removesuffix('.part')
                if FILE_ID.fullmatch(file_id):
                    orphan.unlink(missing_ok=True)
        client, peer, post = await self.source(payload['link'])
        channel = await client.get_entity(peer)
        if getattr(channel, 'noforwards', False):
            raise ValueError('源频道已启用内容保护，无法暂存。')
        posts = [post]
        if payload['album'] and post.grouped_id:
            ids = list(range(max(1, post.id - 19), min(2**31, post.id + 20)))
            members = await client.get_messages(peer, ids=ids)
            posts = sorted((p for p in members if p and p.grouped_id == post.grouped_id), key=lambda p: p.id)
            if not 2 <= len(posts) <= MAX_ALBUM or post.id not in {p.id for p in posts}:
                raise ValueError('无法完整读取相册，请重试或只下载单项。')
        items = [(p, media_info(p)) for p in posts]
        written = []
        try:
            for member, item in items:
                file_id = f'{job}-{member.id}'
                temporary = self.root / f'.relay-{file_id}.part'
                destination = self.root / f'relay-{file_id}'
                source = f'https://t.me/c/{peer.channel_id}/{member.id}'
                written.append((temporary, destination))
                temporary.unlink(missing_ok=True)
                while not accounts.runtime_lock.acquire(False):
                    await asyncio.sleep(0.05)
                try:
                    try:
                        await self.bot.tdl.download_media(source, str(temporary))
                    except ValueError:
                        raise ValueError('TDL 无法下载媒体。请核对 TDL 登录、源频道访问权限和暂存空间。') from None
                finally:
                    accounts.runtime_lock.release()
                info = temporary.lstat()
                size = expected_size(member)
                if not stat.S_ISREG(info.st_mode) or not info.st_size or (size and info.st_size != size):
                    raise ValueError('下载文件不完整。')
                temporary.replace(destination)
                item.update(id=file_id, size=info.st_size, link=source + ('?single' if post.grouped_id else ''))
            with closing(self.connect()) as db, db:
                db.executemany('INSERT INTO files(id,job,payload) VALUES(?,?,?)',
                               [(item['id'], job, json.dumps(item)) for _, item in items])
                self.finish(db, job, 'done', f'下载完成，已暂存 {len(items)} 项。')
        except BaseException:
            for temporary, destination in written:
                temporary.unlink(missing_ok=True)
                destination.unlink(missing_ok=True)
            raise
        return f'下载完成，已暂存 {len(items)} 项。'

    async def upload_staged(self, target, item):
        with self.open_media(item) as stream:
            uploaded_file = await self.bot.client.upload_file(stream, file_size=item['size'], file_name=item['name'])
        if item['kind'] == 'photo':
            uploaded = types.InputMediaUploadedPhoto(uploaded_file)
        else:
            attrs = [types.DocumentAttributeFilename(item['name'])]
            if video := item.get('video'):
                attrs.append(types.DocumentAttributeVideo(video[0], video[1], video[2], supports_streaming=True))
            uploaded = types.InputMediaUploadedDocument(uploaded_file, item['mime'], attrs)
        prepared = await self.bot.client(functions.messages.UploadMediaRequest(target, uploaded))
        return utils.get_input_media(prepared)

    async def publish(self, job, owner, payload):
        client = self.bot.client
        channel = numeric_channel(str(payload['target']))
        if channel is None:
            raise ValueError('目标频道无效，请重新选择。')
        target = await self.bot.get_monitor_channel_peer(client, channel)
        prepared = []
        for item in payload['items']:
            if item['source'] == 'staging':
                stored = self.get(item['ref'])
                if not stored:
                    raise ValueError('暂存文件已不存在，请重新选择。')
                media = await self.upload_staged(target, stored)
            else:
                post = await client.get_messages(owner, ids=item['ref'])
                if not post or post.sender_id != owner:
                    raise ValueError('Bot 私聊中的原文件已不可用，请重新发送。')
                media_info(post)
                media = utils.get_input_media(post.media)
            if not isinstance(media, (types.InputMediaPhoto, types.InputMediaDocument)):
                raise ValueError('无法生成可发布的媒体引用。')
            prepared.append(media)
        reserved = dict(enumerate(payload['random_ids']))
        caption = payload['caption']
        if len(prepared) == 1:
            request = functions.messages.SendMediaRequest(target, prepared[0], caption, random_id=reserved[0])
        else:
            members = [types.InputSingleMedia(media, message=caption if index == 0 else '', random_id=reserved[index])
                       for index, media in enumerate(prepared)]
            request = functions.messages.SendMultiMediaRequest(target, members)
        if not self.bot.admin_panel.allowed(owner):
            raise ValueError('任务发起人已失去管理员权限。')
        with closing(self.connect()) as db, db:
            self.finish(db, job, 'sending', '')
        try:
            response = await client(request)
        except (errors.ChatWriteForbiddenError, errors.ChatAdminRequiredError, errors.ChannelPrivateError,
                errors.FileReferenceExpiredError) as exc:
            message = ('媒体引用已失效，请将原文件重新发送给 Bot。' if isinstance(exc, errors.FileReferenceExpiredError)
                       else 'Bot 无法向目标频道发帖，请检查频道权限。')
            with closing(self.connect()) as db, db:
                self.finish(db, job, 'failed', message)
            raise ValueError(message) from exc
        receipt = receipt_pairs(response, reserved)
        if len(receipt) != len(prepared):
            raise ValueError('Telegram 未返回完整发布回执。')
        links = [post_url(channel, receipt[index], album=len(prepared) > 1) for index in reserved]
        result = f'已发布到 {self.bot.channel_label(channel)}\n' + '\n'.join(links)
        with closing(self.connect()) as db, db:
            self.finish(db, job, 'done', result)
        return result

    @staticmethod
    def publication(draft):
        return {'target': draft.target, 'items': draft.items, 'caption': draft.caption,
                'random_ids': [secrets.randbits(63) or 1 for _ in draft.items]}

    @staticmethod
    def finish(db, job, state, result):
        db.execute('UPDATE jobs SET state=?,result=? WHERE id=?', (state, result, job))

    async def run(self):
        while True:
            self.wake.clear()
            with closing(self.connect()) as db, db:
                row = db.execute("SELECT id,owner,kind,payload,attempts FROM jobs WHERE state='pending' "
                                 'AND ready<=? ORDER BY id LIMIT 1', (time.time(),)).fetchone()
                if row:
                    self.finish(db, row[0], 'working', '')
                else:
                    next_ready = db.execute("SELECT min(ready) FROM jobs WHERE state='pending'").fetchone()[0]
            if row is None:
                if next_ready is None:
                    await self.wake.wait()
                else:
                    try:
                        await asyncio.wait_for(self.wake.wait(), max(0.01, next_ready - time.time()))
                    except TimeoutError:
                        pass
                continue
            job, owner, kind, raw, attempts = row
            payload = json.loads(raw)
            try:
                if not self.bot.admin_panel.allowed(owner):
                    raise ValueError('任务发起人已失去管理员权限。')
                result = await (self.download(job, payload) if kind == 'download' else self.publish(job, owner, payload))
                completed = True
            except errors.FloodWaitError as exc:
                completed = False
                with closing(self.connect()) as db, db:
                    result = f'Telegram 限流，要求等待 {exc.seconds} 秒。'
                    if attempts < 3:
                        db.execute("UPDATE jobs SET state='pending',ready=?,attempts=attempts+1,result=? WHERE id=?",
                                   (time.time() + max(1, exc.seconds), result, job))
                        continue
                    self.finish(db, job, 'failed', result)
            except Exception as exc:
                completed = False
                logger.warning('文件任务 %s 未完成（%s）', job, type(exc).__name__)
                with closing(self.connect()) as db, db:
                    sending = db.execute('SELECT state FROM jobs WHERE id=?', (job,)).fetchone()[0] == 'sending'
                    result = ('发布结果待核对，请检查目标频道再决定是否重新发布。' if sending else
                              str(exc)[:360] if isinstance(exc, ValueError) else '文件任务失败，请核对账号权限和暂存空间。')
                    self.finish(db, job, 'review' if sending else 'failed', result)
            if self.bot.admin_panel.allowed(owner):
                try:
                    if kind == 'download' and completed:
                        if await self.bot.admin_panel.file_download_ready(owner, job, result):
                            continue
                        result += ' 打开「📥 下载暂存」可选用。'
                    await asyncio.wait_for(self.bot.client.send_message(
                        owner, f'文件任务 #{job}\n{result}', parse_mode=None, link_preview=False), 10)
                except Exception:
                    logger.warning('文件任务 %s 的 Bot 通知未送达', job)
