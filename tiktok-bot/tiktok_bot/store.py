"""Состояние бота в SQLite: какие посты уже обработаны, токены TikTok, пауза, статистика роликов. Один файл в томе /data."""
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  chat_id INTEGER NOT NULL,
  message_id INTEGER NOT NULL,
  status TEXT NOT NULL,           -- rendered | inbox | failed | skipped (paused — у старых записей)
  publish_id TEXT,
  error TEXT,
  title TEXT,
  updated_at REAL NOT NULL,
  PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stats (
  video_id TEXT NOT NULL,
  fetched_at REAL NOT NULL,
  age_hours REAL NOT NULL,        -- сколько часов ролику в TikTok на момент замера
  views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER,
  PRIMARY KEY (video_id, fetched_at)
);
"""
# Колонки posts, появившиеся позже первой версии: в старом файле их дописываем при запуске.
POST_COLUMNS = {
    "channel": "TEXT",              # @имя канала, без @ — для ссылки t.me/<channel>/<message_id>
    "kind": "TEXT",                 # text | photo | video | gif — что было в посте
    "format": "TEXT",               # shot (скриншот поста) | slides — как собран ролик
    "text_chars": "INTEGER",
    "seconds": "REAL",              # длительность ролика
    "uploaded_at": "REAL",          # когда ушёл во «Входящие» TikTok
    "video_id": "TEXT",             # id опубликованного ролика в TikTok
    "tiktok_created_at": "REAL",    # когда владелец его опубликовал
    "tiktok_url": "TEXT",
}


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        have = {row["name"] for row in self.db.execute("PRAGMA table_info(posts)")}
        with self.db:
            for name, kind in POST_COLUMNS.items():
                if name not in have:
                    self.db.execute(f"ALTER TABLE posts ADD COLUMN {name} {kind}")
            # ролики, загруженные до учёта времени загрузки: лучшая оценка — время последнего изменения
            self.db.execute("UPDATE posts SET uploaded_at = updated_at WHERE uploaded_at IS NULL AND publish_id IS NOT NULL")

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value) -> None:
        with self.db:
            self.db.execute("INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))

    def seen(self, chat_id: int, message_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM posts WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)).fetchone() is not None

    def mark(self, chat_id: int, message_id: int, status: str, *, error: str | None = None, **fields) -> None:
        """Новый статус поста. error перезаписывается всегда (None — ошибки нет), остальные поля — только переданные."""
        fields = {k: v for k, v in fields.items() if v is not None}
        unknown = set(fields) - {"publish_id", "title", *POST_COLUMNS}
        if unknown:
            raise ValueError(f"неизвестные поля: {', '.join(sorted(unknown))}")
        names = ["chat_id", "message_id", "status", "error", "updated_at", *fields]
        updates = ", ".join(f"{n} = excluded.{n}" for n in names[2:])
        with self.db:
            self.db.execute(
                f"INSERT INTO posts({', '.join(names)}) VALUES ({', '.join('?' * len(names))}) "
                f"ON CONFLICT(chat_id, message_id) DO UPDATE SET {updates}",
                (chat_id, message_id, status, error, time.time(), *fields.values()),
            )

    def update(self, chat_id: int, message_id: int, **fields) -> None:
        """Дописывает сведения о ролике, не трогая статус."""
        unknown = set(fields) - set(POST_COLUMNS)
        if unknown:
            raise ValueError(f"неизвестные поля: {', '.join(sorted(unknown))}")
        with self.db:
            self.db.execute(f"UPDATE posts SET {', '.join(f'{k} = ?' for k in fields)} WHERE chat_id = ? AND message_id = ?",
                            (*fields.values(), chat_id, message_id))

    def recent(self, limit: int = 5) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM posts ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()

    def awaiting_video(self, since: float) -> list[sqlite3.Row]:
        """Ролики во «Входящих», про которые ещё неизвестно, опубликовал ли их владелец."""
        return self.db.execute("SELECT * FROM posts WHERE publish_id IS NOT NULL AND video_id IS NULL AND uploaded_at >= ? "
                               "ORDER BY uploaded_at", (since,)).fetchall()

    def tracked(self, since: float) -> list[sqlite3.Row]:
        """Опубликованные ролики, за которыми ещё следим (опубликованы не раньше since или время публикации неизвестно)."""
        return self.db.execute("SELECT * FROM posts WHERE video_id IS NOT NULL AND (tiktok_created_at IS NULL OR tiktok_created_at >= ?)",
                               (since,)).fetchall()

    def taken_videos(self) -> frozenset[str]:  # не set[str]: внутри класса set — метод хранилища
        return frozenset(row["video_id"] for row in self.db.execute("SELECT video_id FROM posts WHERE video_id IS NOT NULL"))

    def add_stats(self, video_id: str, age_hours: float, views: int | None, likes: int | None, comments: int | None,
                  shares: int | None, fetched_at: float | None = None) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO stats VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (video_id, fetched_at or time.time(), age_hours, views, likes, comments, shares))

    def snapshots(self, video_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM stats WHERE video_id = ? ORDER BY fetched_at", (video_id,)).fetchall()

    def all_posts(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM posts ORDER BY COALESCE(uploaded_at, updated_at)").fetchall()
