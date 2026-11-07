"""同步编排：全量、增量、按需回填、覆盖度判断。"""

from __future__ import annotations

import time
from typing import Callable

from .api import DEFAULT_LIMIT, HeyboxClient
from .models import Page, SyncReport, SyncState
from .store import Store

Logger = Callable[[str], None] | None

# 全量同步的安全上限：即使接口异常也不会无限翻页
MAX_PAGES = 500
# 最近 10 分钟内同步过，就不再为了“查今天”而重复请求
RECENT_TTL = 600


class Syncer:
    def __init__(
        self,
        store: Store,
        client: HeyboxClient,
        userid: int,
        *,
        limit: int = DEFAULT_LIMIT,
        log: Logger = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.client = client
        self.userid = userid
        self.limit = limit
        self.log = log
        self._now = now

    # ---- 内部 -----------------------------------------------------

    def _log(self, message: str) -> None:
        if self.log is not None:
            self.log(message)

    def _fetch(self, offset: int, report: SyncReport) -> tuple[Page, int]:
        page = self.client.fetch_page(self.userid, offset, self.limit)
        inserted, updated = self.store.upsert_posts(page.posts)
        report.pages += 1
        report.fetched += len(page.posts)
        report.inserted += inserted
        report.updated += updated
        self.store.refresh_bounds(self.userid)
        self._log(
            f"offset={offset} · 本页 {len(page.posts)} 条 · 新收录 {inserted} 条 · 累计 {report.inserted} 条"
        )
        return page, inserted

    def _advance(self, state: SyncState, offset: int, page: Page) -> None:
        state.next_offset = max(state.next_offset, offset)
        if page.total_page:
            state.total_page = page.total_page
        if page.post_link_num:
            state.post_link_num = page.post_link_num
        state.last_sync_at = int(self._now())
        self.store.save_state(state)

    # ---- 对外 -----------------------------------------------------

    def sync_full(self, max_pages: int | None = None) -> SyncReport:
        """从断点一直翻到最早一页；已经抓完时退化为刷新最新几页。"""
        state = self.store.load_state(self.userid)
        if state.reached_end:
            return self.sync_latest(max_pages=3)

        report = SyncReport()
        offset = state.next_offset
        pages = 0

        while pages < (max_pages or MAX_PAGES):
            page, _ = self._fetch(offset, report)
            pages += 1

            if not page.posts:
                state.reached_end = True
                break

            offset += self.limit
            self._advance(state, offset, page)

            if page.total_page and offset >= page.total_page * self.limit:
                state.reached_end = True
                break

        state.last_sync_at = int(self._now())
        self.store.save_state(state)
        report.reached_end = state.reached_end
        return report

    def sync_latest(self, max_pages: int = 3) -> SyncReport:
        """只刷新最新的几页，遇到整页都已收录就停。"""
        state = self.store.load_state(self.userid)
        report = SyncReport()
        offset = 0

        for _ in range(max_pages):
            page, inserted_here = self._fetch(offset, report)
            if not page.posts:
                state.reached_end = True
                break

            offset += self.limit
            self._advance(state, offset, page)

            if page.total_page and offset >= page.total_page * self.limit:
                state.reached_end = True
                break
            if inserted_here == 0:
                break

        state.last_sync_at = int(self._now())
        self.store.save_state(state)
        report.reached_end = state.reached_end
        return report

    def backfill(self, target_ts: int, max_pages: int | None = None) -> SyncReport:
        """从 next_offset 继续向更早翻，直到覆盖 target_ts 或到最早一页。"""
        state = self.store.load_state(self.userid)
        coverage = self.store.coverage(self.userid)
        report = SyncReport()

        if coverage.reached_end:
            report.reached_end = True
            return report
        if coverage.oldest is not None and coverage.oldest <= target_ts:
            return report

        offset = state.next_offset
        pages = 0

        while pages < (max_pages or MAX_PAGES):
            page, _ = self._fetch(offset, report)
            pages += 1

            if not page.posts:
                state.reached_end = True
                break

            offset += self.limit
            self._advance(state, offset, page)

            oldest_in_page = min(post.create_at for post in page.posts)
            if oldest_in_page <= target_ts:
                break
            if page.total_page and offset >= page.total_page * self.limit:
                state.reached_end = True
                break

        state.last_sync_at = int(self._now())
        self.store.save_state(state)
        report.reached_end = state.reached_end
        return report

    def ensure_range(self, start_ts: int, end_ts: int) -> SyncReport:
        """查询前的自愈：最近范围先刷新，覆盖不足再回填。"""
        report = SyncReport()
        now = int(self._now())
        coverage = self.store.coverage(self.userid)

        is_recent = end_ts >= now - RECENT_TTL
        is_stale = coverage.last_sync_at is None or now - coverage.last_sync_at > RECENT_TTL
        if is_recent and is_stale:
            report.merge(self.sync_latest())
            coverage = self.store.coverage(self.userid)

        needs_backfill = coverage.oldest is None or (
            start_ts < coverage.oldest and not coverage.reached_end
        )
        if needs_backfill:
            report.merge(self.backfill(start_ts))

        return report
