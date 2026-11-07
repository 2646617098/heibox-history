"""同步编排：全量、增量、按需回填、覆盖度判断。"""

from __future__ import annotations

import time
from typing import Callable

from .api import DEFAULT_LIMIT, HeyboxClient
from .errors import HeiboxError
from .images import ImageCache, thumb_url
from .models import Page, Post, SyncReport, SyncState
from .store import Store

Logger = Callable[[str], None] | None

# 全量同步的安全上限：即使接口异常也不会无限翻页
MAX_PAGES = 500
# 最近 10 分钟内同步过，就不再为了“查今天”而重复请求
RECENT_TTL = 600
# 图片档位：off 不下载，thumb 只存列表缩略图，full 再额外存每帖首张原图
IMAGE_MODES = ("off", "thumb", "full")

Progress = Callable[[int, int, int, int], None] | None


def download_thumbnails(
    client: HeyboxClient,
    images: ImageCache,
    sources: list[tuple[int, str]],
    *,
    on_progress: Progress = None,
) -> tuple[int, int]:
    """按 (linkid, 封面地址) 批量补缩略图，返回 (成功, 失败)。

    只请求图床，不碰帖子列表接口；单张失败继续，交给下次重试。
    """
    saved = failed = 0
    total = len(sources)
    for index, (linkid, source) in enumerate(sources, 1):
        try:
            data, _ctype = client.fetch_image(thumb_url(source))
            ok = images.save_thumb(linkid, data)
        except (HeiboxError, OSError):
            ok = False
        if ok:
            saved += 1
        else:
            failed += 1
        if on_progress is not None and (index == total or index % 5 == 0):
            on_progress(index, total, saved, failed)
    return saved, failed


class Syncer:
    def __init__(
        self,
        store: Store,
        client: HeyboxClient,
        userid: int,
        *,
        limit: int = DEFAULT_LIMIT,
        log: Logger = None,
        image_mode: str = "off",
        images: ImageCache | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.client = client
        self.userid = userid
        self.limit = limit
        self.log = log
        self.image_mode = image_mode if image_mode in IMAGE_MODES else "off"
        self.images = images
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
        saved, failed = self._cache_images(page.posts)
        report.images += saved
        report.images_failed += failed
        self.store.refresh_bounds(self.userid)
        image_note = ""
        if self.image_mode != "off" and (saved or failed):
            image_note = f" · 图 +{saved}" + (f"（失败 {failed}）" if failed else "")
        self._log(
            f"offset={offset} · 本页 {len(page.posts)} 条 · 新收录 {inserted} 条"
            f" · 累计 {report.inserted} 条{image_note}"
        )
        return page, inserted

    def _cache_images(self, posts: tuple[Post, ...]) -> tuple[int, int]:
        """按当前档位把封面写到本地；单张失败只计数，不打断同步。"""
        if self.images is None or self.image_mode == "off" or not posts:
            return (0, 0)

        existing_full = self.store.image_files([post.linkid for post in posts])
        saved = failed = 0

        for post in posts:
            if post.thumb_url and not self.images.has_thumb(post.linkid):
                try:
                    data, _ctype = self.client.fetch_image(thumb_url(post.thumb_url))
                except (HeiboxError, OSError):
                    failed += 1
                else:
                    if self.images.save_thumb(post.linkid, data):
                        saved += 1
                    else:
                        failed += 1

            if (
                self.image_mode == "full"
                and post.image_url
                and not existing_full.get(post.linkid)
            ):
                try:
                    data, ctype = self.client.fetch_image(post.image_url)
                    relative = self.images.save_full(post.linkid, data, ctype)
                except (HeiboxError, OSError):
                    failed += 1
                else:
                    self.store.set_image_file(post.linkid, relative)
                    saved += 1

        return saved, failed

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
