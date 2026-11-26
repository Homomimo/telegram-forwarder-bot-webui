"""Persistent admissions for one bot process. Never store events, peers, or credentials."""
import json
import sqlite3
from pathlib import Path


from channel_input import numeric_channel

FIELDS = ('link', 'status_msg_id', 'chat_id', 'target_chat_id', 'created_at', 'grouped_id',
          'mode', 'origin', 'message_ids', 'source_chat_id', 'retries', 'store_retries', 'ready_at', 'observer',
          'verify_edit', 'edit_media_intent', 'refused_role', 'album_event_ids', 'manual_meta')


class TaskJournal:
    def __init__(self, map_path):
        self.path = Path(str(map_path) + '.queue.sqlite3')
        self.db = None

    def open(self):
        if self.db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path)
            try:
                db.execute('CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, '
                           'state TEXT NOT NULL, payload TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS manual_members(job INTEGER, source INTEGER, result INTEGER, '
                           'PRIMARY KEY(job,source))')
                db.execute('CREATE TABLE IF NOT EXISTS admin_panels(bot INTEGER, owner INTEGER, mid INTEGER, '
                           'kind TEXT NOT NULL, active INTEGER NOT NULL, PRIMARY KEY(bot,owner,mid))')
                db.execute('CREATE TABLE IF NOT EXISTS admin_panel_replies(bot INTEGER, owner INTEGER, mid INTEGER, '
                           'PRIMARY KEY(bot,owner,mid))')
            except sqlite3.Error:
                db.close()
                raise
            self.db = db
        return self.db

    def reusable_panel(self, bot, owner):
        row = self.open().execute("SELECT mid FROM admin_panels WHERE bot=? AND owner=? AND active=1 "
                                  "AND kind='panel'", (bot, owner)).fetchone()
        return row[0] if row else 0

    def is_active_panel(self, bot, owner, mid):
        return self.open().execute('SELECT 1 FROM admin_panels WHERE bot=? AND owner=? AND mid=? AND active=1',
                                   (bot, owner, mid)).fetchone() is not None

    def panel_reply_handled(self, bot, owner, mid):
        return self.open().execute('SELECT 1 FROM admin_panel_replies WHERE bot=? AND owner=? AND mid=?',
                                   (bot, owner, mid)).fetchone() is not None

    def remember_panel_reply(self, bot, owner, mid):
        with self.open() as db:
            db.execute('INSERT OR IGNORE INTO admin_panel_replies VALUES(?,?,?)', (bot, owner, mid))
            db.execute('DELETE FROM admin_panel_replies WHERE bot=? AND owner=? AND mid < ('
                       'SELECT mid FROM admin_panel_replies WHERE bot=? AND owner=? '
                       'ORDER BY mid DESC LIMIT 1 OFFSET 127)', (bot, owner, bot, owner))

    def activate_panel(self, bot, owner, mid, kind):
        # Commit the new pointer and the outstanding deletions before contacting Telegram again.
        with self.open() as db:
            db.execute('UPDATE admin_panels SET active=0 WHERE bot=? AND owner=? AND active=1', (bot, owner))
            db.execute('INSERT OR REPLACE INTO admin_panels VALUES(?,?,?,?,1)', (bot, owner, mid, kind))
            return db.execute('SELECT mid,kind FROM admin_panels WHERE bot=? AND owner=? AND active=0',
                              (bot, owner)).fetchall()

    def forget_retired_panel(self, bot, owner, mid):
        with self.open() as db:
            db.execute('DELETE FROM admin_panels WHERE bot=? AND owner=? AND mid=? AND active=0', (bot, owner, mid))

    def restore(self):
        with self.open() as db:
            return [(jid, state, json.loads(payload)) for jid, state, payload in db.execute(
                "SELECT id,state,payload FROM jobs ORDER BY id")]

    def save(self, task, state='pending'):
        with self.open() as db:
            jid = self._save(db, task, state)
        task.job_id = jid

    def _save(self, db, task, state):
        payload = json.dumps({key: getattr(task, key) for key in FIELDS}, separators=(',', ':'))
        if task.job_id is None:
            return db.execute('INSERT INTO jobs(state,payload) VALUES(?,?)', (state, payload)).lastrowid
        elif not db.execute('UPDATE jobs SET state=?,payload=? WHERE id=?', (state, payload, task.job_id)).rowcount:
            raise RuntimeError('任务的持久化记录不存在')
        return task.job_id

    def admit_web(self, task, key):
        # Persist the receipt and admission together, before waking the worker.
        signature = json.dumps([task.mode, task.link, task.message_ids, task.target_chat_id])
        with self.open() as db:
            db.execute('CREATE TABLE IF NOT EXISTS web_requests(key TEXT PRIMARY KEY, job INTEGER, signature TEXT)')
            existing = db.execute('SELECT job,signature FROM web_requests WHERE key=?', (key,)).fetchone()
            if existing:
                if existing[1] != signature:
                    # A preview.2 request may have stored a marked destination ID.
                    old = json.loads(existing[1])
                    if (old[:3] != [task.mode, task.link, task.message_ids]
                            or numeric_channel(str(old[3])) != numeric_channel(str(task.target_chat_id))):
                        raise ValueError('提交标识已用于不同任务，请重新打开表单')
                return existing[0], False
            task.job_id = self._save(db, task, 'pending')
            db.execute('INSERT INTO web_requests VALUES(?,?,?)', (key, task.job_id, signature))
        return task.job_id, True

    def prepare_manual(self, task, members):
        with self.open() as db:
            jid = self._save(db, task, 'sending')
            db.executemany('INSERT INTO manual_members VALUES(?,?,?)',
                           ((jid, mid, result) for mid, result in (members or {}).items()))
        task.job_id = jid

    def mark_manual(self, task, changes):
        with self.open() as db:
            db.executemany('UPDATE manual_members SET result=? WHERE job=? AND source=?',
                           ((value, task.job_id, mid) for mid, value in changes.items()))

    def manual_members(self, task):
        return dict(self.open().execute('SELECT source,result FROM manual_members WHERE job=? ORDER BY source',
                                        (task.job_id,)))

    def finish_manual(self, task, review):
        with self.open() as db:
            db.execute('DELETE FROM manual_members WHERE job=?', (task.job_id,))
            if review:
                self._save(db, task, 'review')
            else:
                db.execute('DELETE FROM jobs WHERE id=?', (task.job_id,))

    def discard(self, ids):
        ids = [(jid,) for jid in ids if jid is not None]
        if ids:
            with self.open() as db:
                db.executemany('DELETE FROM jobs WHERE id=?', ids)

    def cancel_waiters(self, current_id, timer_ids, stop_current=False):
        with self.open() as db:
            # Cancel the future verification, not the evidence of an earlier in-flight edit.
            db.execute("UPDATE jobs SET state='review' WHERE state IN ('pending','wait_peer','wait_store') AND id IS NOT ? "
                       "AND json_extract(payload,'$.mode')='monitor_edit' AND json_extract(payload,'$.verify_edit')=1",
                       (current_id,))
            db.execute("DELETE FROM jobs WHERE state IN ('pending','early','wait_peer','wait_album','wait_store') AND id IS NOT ?", (current_id,))
            # Those rows are the only recovery handle if cancellation cannot release the map reservation.
            db.executemany("DELETE FROM jobs WHERE id=? AND state NOT IN ('review','retry_rejected')", [(jid,) for jid in timer_ids])
            if stop_current:
                db.execute("DELETE FROM jobs WHERE id=? AND state='pending'", (current_id,))
                db.execute("UPDATE jobs SET state='review' WHERE id=? AND state='sending'", (current_id,))

    def reviewed_copy_ids(self, task):
        return {mid for (mid,) in self.open().execute(
            "SELECT member.value FROM jobs, json_each(jobs.payload, '$.message_ids') AS member "
            "WHERE state='review' AND jobs.id != ? "
            "AND json_extract(payload, '$.mode')='monitor_copy' "
            "AND json_extract(payload, '$.source_chat_id')=? AND json_extract(payload, '$.target_chat_id')=?",
            (task.job_id, task.source_chat_id, task.target_chat_id))}

    def resolve_reviews(self, ids=None, *, current_id=None, missing=None) -> int:
        """Acknowledge review rows only; never change delivery intents or mappings."""
        delete = "DELETE FROM jobs WHERE state='review' AND id IS NOT ?"
        skipped = []
        with self.open() as db:
            if ids is None:
                return db.execute(delete, (current_id,)).rowcount
            count = 0
            for jid in dict.fromkeys(ids):
                deleted = db.execute(delete + ' AND id=?', (current_id, jid)).rowcount
                count += deleted
                if not deleted:
                    skipped.append(jid)
        if missing is not None:
            missing.extend(skipped)
        return count

    def reviews(self, before=None, limit=5):
        db = self.open()
        total = db.execute("SELECT count(*) FROM jobs WHERE state='review'").fetchone()[0]
        where, args = (" AND id < ?", (before,)) if before is not None else ("", ())
        recent = [dict(json.loads(payload), job_id=jid) for jid, payload in db.execute(
            "SELECT id,payload FROM jobs WHERE state='review'" + where + " ORDER BY id DESC LIMIT ?", (*args, limit))]
        return total, recent

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
