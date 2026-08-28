"""SQLite persistence for the Nintendo Museum ticket watcher.

Each subscriber is a Telegram chat_id; each subscription is a single date the
user wants tickets for. State survives restarts (cloud hosts redeploy).
"""

import sqlite3
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        with _LOCK:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id     INTEGER NOT NULL,
                    date        TEXT    NOT NULL,          -- YYYY-MM-DD
                    created_at  INTEGER NOT NULL,
                    last_status TEXT,                      -- available/soldout/closed/unknown
                    notified    INTEGER DEFAULT 0,         -- 1 while already alerted
                    UNIQUE(chat_id, date)
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            self.conn.commit()

    def add_subscription(self, chat_id, date) -> bool:
        with _LOCK:
            try:
                self.conn.execute(
                    "INSERT INTO subscriptions(chat_id, date, created_at) VALUES(?,?,?)",
                    (chat_id, date, int(time.time())),
                )
                self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def list_subscriptions(self, chat_id):
        with _LOCK:
            return self.conn.execute(
                "SELECT * FROM subscriptions WHERE chat_id=? ORDER BY date", (chat_id,)
            ).fetchall()

    def all_subscriptions(self):
        with _LOCK:
            return self.conn.execute("SELECT * FROM subscriptions").fetchall()

    def delete_subscription(self, chat_id, sub_id) -> bool:
        with _LOCK:
            cur = self.conn.execute(
                "DELETE FROM subscriptions WHERE id=? AND chat_id=?", (sub_id, chat_id)
            )
            self.conn.commit()
            return cur.rowcount > 0

    def clear_subscriptions(self, chat_id) -> int:
        with _LOCK:
            cur = self.conn.execute(
                "DELETE FROM subscriptions WHERE chat_id=?", (chat_id,)
            )
            self.conn.commit()
            return cur.rowcount

    def update_state(self, sub_id, last_status, notified):
        with _LOCK:
            self.conn.execute(
                "UPDATE subscriptions SET last_status=?, notified=? WHERE id=?",
                (last_status, notified, sub_id),
            )
            self.conn.commit()

    def set_meta(self, key, value):
        with _LOCK:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            self.conn.commit()

    def get_meta(self, key, default=None):
        with _LOCK:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else default
