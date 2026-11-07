"""SQLite 存储：帖子、同步断点、覆盖度。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .errors import ConfigError
from .models import Coverage, Post, SyncState

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    linkid      INTEGER PRIMARY KEY,
    userid      INTEGER NOT NULL,
    create_at   INTEGER NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    topics      TEXT NOT NULL DEFAULT '[]',
    url         TEXT NOT NULL DEFAULT '',
    link_type   INTEGER NOT NULL DEFAULT 0,
    click       INTEGER NOT NULL DEFAULT 0,
    comment_num INTEGER NOT NULL DEFAULT 0,
    like_num    INTEGER
);

CREATE INDEX IF NOT EXISTS ix_posts_time ON posts(userid, create_at DESC);

CREATE TABLE IF NOT EXISTS sync_state (
    userid           INTEGER PRIMARY KEY,
    oldest_create_at INTEGER,
    newest_create_at INTEGER,
    next_offset      INTEGER NOT NULL DEFAULT 0,
    reached_end      INTEGER NOT NULL DEFAULT 0,
    total_page       INTEGER,
    post_link_num    INTEGER,
    last_sync_at     INTEGER
);
"""

UPSERT_POST = """
INSERT INTO posts (linkid, userid, create_at, title, description, topics, url, link_type, click, comment_num, like_num)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(linkid) DO UPDATE SET
    title       = excluded.title,
    description = excluded.description,
    topics      = excluded.topics,
    url         = excluded.url,
    link_type   = excluded.link_type,
    click       = excluded.click,
    comment_num = excluded.comment_num,
    like_num    = excluded.like_num
"""

UPSERT_STATE = """
INSERT INTO sync_state (userid, next_offset, reached_end, total_page, post_link_num, last_sync_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(userid) DO UPDATE SET
    next_offset   = excluded.next_offset,
    reached_end   = excluded.reached_end,
    total_page    = excluded.total_page,
    post_link_num = excluded.post_link_num,
    last_sync_at  = excluded.last_sync_at
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        """老版本数据库补列：CREATE TABLE IF NOT EXISTS 不会改已有表结构。"""
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(posts)")}
        if "like_num" not in columns:
            self._db.execute("ALTER TABLE posts ADD COLUMN like_num INTEGER")

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- 用户绑定 -------------------------------------------------

    def ensure_user(self, userid: int) -> None:
        row = self._db.execute("SELECT value FROM meta WHERE key='userid'").fetchone()
        if row is None:
            self._db.execute("INSERT INTO meta(key, value) VALUES('userid', ?)", (str(userid),))
            self._db.commit()
            return
        if int(row["value"]) != userid:
            raise ConfigError(
                f"数据库绑定的是 userid={row['value']}，与当前配置 {userid} 不一致；"
                "如需更换账号，请换一个 --data-dir"
            )

    # ---- 帖子 -----------------------------------------------------

    def upsert_posts(self, posts: Iterable[Post]) -> tuple[int, int]:
        batch = list(posts)
        if not batch:
            return (0, 0)

        ids = [post.linkid for post in batch]
        placeholders = ",".join("?" * len(ids))
        existing = {
            row["linkid"]
            for row in self._db.execute(
                f"SELECT linkid FROM posts WHERE linkid IN ({placeholders})", ids
            )
        }
        self._db.executemany(
            UPSERT_POST,
            [
                (
                    post.linkid,
                    post.userid,
                    post.create_at,
                    post.title,
                    post.description,
                    json.dumps(list(post.topics), ensure_ascii=False),
                    post.url,
                    post.link_type,
                    post.click,
                    post.comment_num,
                    post.like_num,
                )
                for post in batch
            ],
        )
        self._db.commit()
        inserted = sum(1 for linkid in ids if linkid not in existing)
        return inserted, len(ids) - inserted

    def query(self, userid: int, start: int, end: int, limit: int, offset: int = 0) -> list[Post]:
        rows = self._db.execute(
            """
            SELECT * FROM posts
            WHERE userid = ? AND create_at BETWEEN ? AND ?
            ORDER BY create_at DESC, linkid DESC
            LIMIT ? OFFSET ?
            """,
            (userid, start, end, limit, offset),
        ).fetchall()
        return [_row_to_post(row) for row in rows]

    def count(self, userid: int, start: int, end: int) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS total FROM posts WHERE userid = ? AND create_at BETWEEN ? AND ?",
            (userid, start, end),
        ).fetchone()
        return int(row["total"])

    def get_post(self, linkid: int) -> Post | None:
        row = self._db.execute("SELECT * FROM posts WHERE linkid = ?", (linkid,)).fetchone()
        return _row_to_post(row) if row else None

    # ---- 同步状态 -------------------------------------------------

    def load_state(self, userid: int) -> SyncState:
        self._db.execute("INSERT OR IGNORE INTO sync_state(userid) VALUES(?)", (userid,))
        self._db.commit()
        row = self._db.execute("SELECT * FROM sync_state WHERE userid = ?", (userid,)).fetchone()
        return SyncState(
            userid=userid,
            next_offset=int(row["next_offset"]),
            reached_end=bool(row["reached_end"]),
            total_page=row["total_page"],
            post_link_num=row["post_link_num"],
            last_sync_at=row["last_sync_at"],
        )

    def save_state(self, state: SyncState) -> None:
        self._db.execute(
            UPSERT_STATE,
            (
                state.userid,
                state.next_offset,
                int(state.reached_end),
                state.total_page,
                state.post_link_num,
                state.last_sync_at,
            ),
        )
        self._db.commit()
        self.refresh_bounds(state.userid)

    def refresh_bounds(self, userid: int) -> None:
        row = self._db.execute(
            "SELECT MIN(create_at) AS oldest, MAX(create_at) AS newest, COUNT(*) AS total "
            "FROM posts WHERE userid = ?",
            (userid,),
        ).fetchone()
        if row["total"]:
            self._db.execute(
                "UPDATE sync_state SET oldest_create_at = ?, newest_create_at = ? WHERE userid = ?",
                (row["oldest"], row["newest"], userid),
            )
            self._db.commit()

    def coverage(self, userid: int) -> Coverage:
        counts = self._db.execute(
            "SELECT MIN(create_at) AS oldest, MAX(create_at) AS newest, COUNT(*) AS total "
            "FROM posts WHERE userid = ?",
            (userid,),
        ).fetchone()
        state = self._db.execute(
            "SELECT reached_end, last_sync_at, oldest_create_at, newest_create_at "
            "FROM sync_state WHERE userid = ?",
            (userid,),
        ).fetchone()

        oldest = counts["oldest"] if counts["oldest"] is not None else (
            state["oldest_create_at"] if state else None
        )
        newest = counts["newest"] if counts["newest"] is not None else (
            state["newest_create_at"] if state else None
        )
        return Coverage(
            oldest=oldest,
            newest=newest,
            reached_end=bool(state["reached_end"]) if state else False,
            total=int(counts["total"]),
            last_sync_at=state["last_sync_at"] if state else None,
        )


def _row_to_post(row: sqlite3.Row) -> Post:
    return Post(
        linkid=row["linkid"],
        userid=row["userid"],
        create_at=row["create_at"],
        title=row["title"],
        description=row["description"],
        topics=tuple(json.loads(row["topics"] or "[]")),
        url=row["url"],
        link_type=row["link_type"],
        click=row["click"],
        comment_num=row["comment_num"],
        like_num=row["like_num"],
    )
