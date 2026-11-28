"""Persistent Telegram downloads and the media catalog."""

import asyncio
from contextlib import closing
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import time

from telethon import errors, types

import accounts
from channel_input import parse_reference
from monitor_clone import expected_size, media_key

logger = logging.getLogger(__name__)
DOWNLOADS = Path('/downloads')
PAGE_SIZE = 7
PREVIEW_PAGE_SIZE = 24
MAX_ALBUM = 10
PREVIEW_TYPES = frozenset({
    'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/avif',
    'video/mp4', 'video/webm', 'video/ogg', 'video/quicktime',
})
FILE_ID = re.compile(r'[1-9]\d{0,18}-[1-9]\d{0,9}')
FILENAME_SEPARATORS = frozenset('/\\:*?"<>|')


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
    key = media_key(post)
    if key.startswith('photo:'):
        return {'name': '照片.jpg', 'mime': 'image/jpeg'}
    if not key.startswith('document:'):
        raise ValueError('该消息没有可用的图片、视频或文件。')
    doc = post.media.document
    name = next((attr.file_name for attr in doc.attributes if isinstance(attr, types.DocumentAttributeFilename)), '')
    name = name.replace('\\', '/').rsplit('/', 1)[-1]
    return {'name': name or '文件', 'mime': doc.mime_type}


def filename_part(value):
    return ''.join('_' if c in FILENAME_SEPARATORS or not c.isprintable() else c for c in value).strip(' .')


def fit_filename(value, limit):
    return value.encode('utf-8')[:limit].decode('utf-8', errors='ignore')


def staged_name(channel, filename, message_id):
    prefix = f'{fit_filename(filename_part(channel), 90)}_{message_id}_'
    name = filename_part(filename) or '文件'
    stem, extension = os.path.splitext(name)
    if len(extension.encode('utf-8')) > 24:
        stem, extension = name, ''
    budget = 240 - len((prefix + extension).encode('utf-8'))
    return (prefix + fit_filename(stem, budget) + extension).rstrip(' .')


def size_label(size):
    if size < 1024:
        return f'{size} B'
    if size < 1024**2:
        return f'{size / 1024:.1f} KB'
    return f'{size / 1024**2:.1f} MB'


class FileStaging:
    def __init__(self, bot, data, root):
        self.bot = bot
        self.root = root
        self.db_path = data / 'file_staging.sqlite3'
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
            # Keep old publication records for verification while preventing any queued send from resuming.
            db.execute("UPDATE jobs SET state='review', result='旧发布任务曾开始发送，请核对目标频道。' "
                       "WHERE kind='publish' AND state='sending'")
            db.execute("UPDATE jobs SET state='failed', result='上传发布功能已移除，任务已取消。' "
                       "WHERE kind='publish' AND state IN ('pending','working')")
            db.execute("UPDATE jobs SET state='pending' WHERE kind='download' AND state='working'")
        self.worker = asyncio.create_task(self.run())

    async def stop(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None

    def enqueue_download(self, owner, link, *, album):
        with closing(self.connect()) as db, db:
            job = db.execute("INSERT INTO jobs(owner,kind,state,payload) VALUES(?,'download','pending',?)",
                             (owner, json.dumps({'link': link, 'album': album}))).lastrowid
        self.wake.set()
        return job

    def recent(self, owner):
        with closing(self.connect()) as db:
            rows = db.execute('SELECT id,kind,state,result FROM jobs WHERE owner=? ORDER BY id DESC LIMIT 5', (owner,))
            return list(rows)

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
        fd = os.open(self.media_path(item), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
                     | getattr(os, 'O_NONBLOCK', 0))
        try:
            status = os.fstat(fd)
            if not stat.S_ISREG(status.st_mode) or status.st_size != item['size']:
                raise OSError('暂存文件已变化')
            return os.fdopen(fd, 'rb')
        except BaseException:
            os.close(fd)
            raise

    def media_path(self, item):
        file_id = item['id']
        if not FILE_ID.fullmatch(file_id):
            raise OSError('无效的暂存文件 ID')
        name = item.get('storage_name')
        if name is None:
            return self.root / f'relay-{file_id}'
        if not isinstance(name, str) or not name or filename_part(name) != name:
            raise OSError('无效的暂存文件名')
        directory = self.root / f'relay-job-{file_id.partition("-")[0]}'
        if directory.is_symlink():
            raise OSError('暂存目录不可用')
        return directory / name

    def cleanup_candidates(self, ids=None):
        with closing(self.connect()) as db:
            rows = db.execute('SELECT id,payload FROM files ORDER BY job DESC,id')
            return [item for item in self.decode_files(rows) if ids is None or item['id'] in ids]

    def clean(self, ids):
        removed, failed = [], 0
        with closing(self.connect()) as db, db:
            for item in self.cleanup_candidates(ids):
                try:
                    path = self.media_path(item)
                    path.unlink(missing_ok=True)
                except OSError:
                    failed += 1
                    continue
                db.execute('DELETE FROM files WHERE id=?', (item['id'],))
                removed.append(item)
                if path.parent != self.root:
                    try:
                        path.parent.rmdir()
                    except OSError:
                        pass  # Other downloads from this job may still be present.
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
        destination = self.root / f'relay-job-{job}'
        for directory in (destination, *self.root.glob(f'.relay-job-{job}-*')):
            if directory.exists() or directory.is_symlink():
                shutil.rmtree(directory)
        client, peer, post = await self.source(payload['link'])
        channel = getattr(getattr(post, 'chat', None), 'title', None)
        if not channel:
            try:
                channel = getattr(await client.get_entity(peer), 'title', None)
            except (ValueError, errors.ChannelPrivateError):
                pass
        channel = filename_part(channel or '') or str(peer.channel_id)
        posts = [post]
        if payload['album'] and post.grouped_id:
            ids = list(range(max(1, post.id - 19), min(2**31, post.id + 20)))
            members = await client.get_messages(peer, ids=ids)
            posts = sorted((p for p in members if p and p.grouped_id == post.grouped_id), key=lambda p: p.id)
            if not 2 <= len(posts) <= MAX_ALBUM or post.id not in {p.id for p in posts}:
                raise ValueError('无法完整读取相册，请重试或只下载单项。')
        items = [(p, media_info(p)) for p in posts]
        with tempfile.TemporaryDirectory(prefix=f'.relay-job-{job}-', dir=self.root) as working:
            working = Path(working)
            for member, item in items:
                file_id = f'{job}-{member.id}'
                name = staged_name(channel, item['name'], member.id)
                temporary = working / f'.{member.id}.part'
                source = f'https://t.me/c/{peer.channel_id}/{member.id}'
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
                temporary.rename(working / name)
                item.update(id=file_id, name=name, storage_name=name, size=info.st_size,
                            link=source + ('?single' if post.grouped_id else ''))
            working.rename(destination)
            try:
                with closing(self.connect()) as db, db:
                    db.executemany('INSERT INTO files(id,job,payload) VALUES(?,?,?)',
                                   [(item['id'], job, json.dumps(item)) for _, item in items])
                    self.finish(db, job, 'done', f'下载完成，已暂存 {len(items)} 项。')
            except BaseException:
                shutil.rmtree(destination)
                raise
        return f'下载完成，已暂存 {len(items)} 项。'

    @staticmethod
    def finish(db, job, state, result):
        db.execute('UPDATE jobs SET state=?,result=? WHERE id=?', (state, result, job))

    async def run(self):
        while True:
            self.wake.clear()
            with closing(self.connect()) as db, db:
                row = db.execute("SELECT id,owner,payload,attempts FROM jobs WHERE kind='download' AND state='pending' "
                                 'AND ready<=? ORDER BY id LIMIT 1', (time.time(),)).fetchone()
                if row:
                    self.finish(db, row[0], 'working', '')
                else:
                    next_ready = db.execute("SELECT min(ready) FROM jobs WHERE kind='download' AND state='pending'").fetchone()[0]
            if row is None:
                if next_ready is None:
                    await self.wake.wait()
                else:
                    try:
                        await asyncio.wait_for(self.wake.wait(), max(0.01, next_ready - time.time()))
                    except TimeoutError:
                        pass
                continue
            job, owner, raw, attempts = row
            payload = json.loads(raw)
            try:
                if not self.bot.admin_panel.allowed(owner):
                    raise ValueError('任务发起人已失去管理员权限。')
                result = await self.download(job, payload)
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
                    result = str(exc)[:360] if isinstance(exc, ValueError) else '下载失败，请核对账号权限和暂存空间。'
                    self.finish(db, job, 'failed', result)
            if self.bot.admin_panel.allowed(owner):
                try:
                    if completed:
                        if await self.bot.admin_panel.file_download_ready(owner, job, result):
                            continue
                        result += ' 打开「📥 下载暂存」查看文件。'
                    await asyncio.wait_for(self.bot.client.send_message(
                        owner, f'文件任务 #{job}\n{result}', parse_mode=None, link_preview=False), 10)
                except Exception:
                    logger.warning('文件任务 %s 的 Bot 通知未送达', job)
