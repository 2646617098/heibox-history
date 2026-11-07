"""领域模型：只保留查询和展示需要的字段，不保存原始 JSON。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Post:
    linkid: int
    userid: int
    create_at: int
    title: str = ""
    description: str = ""
    topics: tuple[str, ...] = ()
    url: str = ""
    link_type: int = 0
    click: int = 0
    comment_num: int = 0
    # 点赞数。小黑盒列表接口里叫 link_award_num，网页端 👍 图标显示的就是它。
    # None 表示这条记录是在加这个字段之前抓的、还没补数据（不是 0 赞）。
    like_num: int | None = None


@dataclass(frozen=True, slots=True)
class Page:
    posts: tuple[Post, ...]
    total_page: int = 0
    post_link_num: int = 0
    profile_userid: int = 0


@dataclass
class SyncState:
    userid: int
    next_offset: int = 0
    reached_end: bool = False
    total_page: int | None = None
    post_link_num: int | None = None
    last_sync_at: int | None = None


@dataclass(frozen=True, slots=True)
class Coverage:
    oldest: int | None
    newest: int | None
    reached_end: bool
    total: int
    last_sync_at: int | None


@dataclass
class SyncReport:
    pages: int = 0
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    reached_end: bool = False

    def merge(self, other: "SyncReport") -> None:
        self.pages += other.pages
        self.fetched += other.fetched
        self.inserted += other.inserted
        self.updated += other.updated
        self.reached_end = self.reached_end or other.reached_end
