from heibox.errors import NetworkError
from heibox.images import ImageCache, thumb_url
from heibox.models import Page, Post
from heibox.sync import Syncer, download_thumbnails
from pngutil import make_png

USERID = 7
PER_PAGE = 20
PNG = make_png(96, 96)
JPEG = b"\xff\xd8full"


def make_pages(count: int = 3) -> list[Page]:
    pages: list[Page] = []
    linkid = 1000
    timestamp = 1_700_000_000
    for _ in range(count):
        posts = []
        for _ in range(PER_PAGE):
            posts.append(
                Post(
                    linkid=linkid,
                    userid=USERID,
                    create_at=timestamp,
                    thumb_url=f"https://img.example.com/{linkid}.jpg?x=1",
                    image_url=f"https://img.example.com/{linkid}-full.jpg",
                )
            )
            linkid += 1
            timestamp -= 3600
        pages.append(Page(tuple(posts), total_page=count, post_link_num=count * PER_PAGE))
    return pages


class FakeClient:
    def __init__(self, pages: list[Page], failing: set[str] | None = None):
        self.pages = pages
        self.calls: list[int] = []
        self.image_calls: list[str] = []
        self.failing = failing or set()

    def fetch_page(self, userid: int, offset: int, limit: int = 60) -> Page:
        self.calls.append(offset)
        index = offset // limit
        if index >= len(self.pages):
            return Page((), total_page=len(self.pages), post_link_num=len(self.pages) * PER_PAGE)
        return self.pages[index]

    def fetch_image(self, url: str) -> tuple[bytes, str]:
        self.image_calls.append(url)
        if url in self.failing:
            raise NetworkError("boom")
        if url.endswith("format/png"):
            return PNG, "image/png"
        return JPEG, "image/jpeg"


def test_sync_full_walks_every_page(store):
    client = FakeClient(make_pages())
    report = Syncer(store, client, USERID).sync_full()

    assert client.calls == [0, 60, 120]
    assert report.pages == 3
    assert report.inserted == 60
    assert report.reached_end is True
    assert store.count(USERID, 0, 10**10) == 60


def test_sync_full_resumes_from_saved_offset(store):
    pages = make_pages()

    Syncer(store, FakeClient(pages), USERID).sync_full(max_pages=1)
    assert store.load_state(USERID).next_offset == 60
    assert store.load_state(USERID).reached_end is False

    second = FakeClient(pages)
    report = Syncer(store, second, USERID).sync_full()

    assert second.calls == [60, 120]
    assert report.inserted == 40
    assert report.reached_end is True


def test_sync_latest_stops_when_page_is_fully_known(store):
    pages = make_pages()
    Syncer(store, FakeClient(pages), USERID).sync_full()

    client = FakeClient(pages)
    report = Syncer(store, client, USERID).sync_latest()

    assert client.calls == [0]
    assert report.inserted == 0


def test_backfill_continues_from_latest_cursor(store):
    pages = make_pages()
    Syncer(store, FakeClient(pages), USERID).sync_latest(max_pages=1)

    target = pages[-1].posts[-1].create_at - 1
    client = FakeClient(pages)
    report = Syncer(store, client, USERID).backfill(target)

    assert client.calls == [60, 120]
    assert report.reached_end is True
    assert store.count(USERID, 0, 10**10) == 60


def test_image_mode_off_never_downloads(store, tmp_path):
    client = FakeClient(make_pages(count=1))
    report = Syncer(
        store, client, USERID, image_mode="off", images=ImageCache(tmp_path)
    ).sync_full()

    assert client.image_calls == []
    assert report.images == 0
    assert report.images_failed == 0


def test_thumb_mode_downloads_each_thumb_only_once(store, tmp_path):
    pages = make_pages(count=1)
    cache = ImageCache(tmp_path)
    client = FakeClient(pages)

    report = Syncer(
        store, client, USERID, image_mode="thumb", images=cache
    ).sync_full()

    assert report.images == PER_PAGE
    assert report.images_failed == 0
    assert len(client.image_calls) == PER_PAGE
    assert all(call.endswith("format/png") for call in client.image_calls)
    assert all(cache.has_thumb(post.linkid) for post in pages[0].posts)

    # 再同步一次（已到最早 → 退化为刷新最新页），文件都在，不应重复下载
    again = FakeClient(pages)
    report = Syncer(
        store, again, USERID, image_mode="thumb", images=cache
    ).sync_full()
    assert again.image_calls == []
    assert report.images == 0


def test_thumb_mode_counts_failures_without_aborting(store, tmp_path):
    pages = make_pages(count=1)
    first = pages[0].posts[0]
    bad_url = thumb_url(first.thumb_url)
    client = FakeClient(pages, failing={bad_url})

    report = Syncer(
        store, client, USERID, image_mode="thumb", images=ImageCache(tmp_path)
    ).sync_full()

    assert report.images == PER_PAGE - 1
    assert report.images_failed == 1
    assert store.count(USERID, 0, 10**10) == PER_PAGE


def test_full_mode_saves_cover_and_remembers_path(store, tmp_path):
    pages = make_pages(count=1)
    cache = ImageCache(tmp_path)
    client = FakeClient(pages)

    report = Syncer(
        store, client, USERID, image_mode="full", images=cache
    ).sync_full()

    assert report.images == PER_PAGE * 2
    assert len(client.image_calls) == PER_PAGE * 2

    post = store.get_post(pages[0].posts[0].linkid)
    assert post is not None
    assert post.image_file == f"images/full/{post.linkid}.jpg"
    assert (tmp_path / post.image_file).read_bytes() == JPEG


def test_full_mode_skips_already_downloaded_originals(store, tmp_path):
    pages = make_pages(count=1)
    cache = ImageCache(tmp_path)
    Syncer(store, FakeClient(pages), USERID, image_mode="full", images=cache).sync_full()

    again = FakeClient(pages)
    report = Syncer(
        store, again, USERID, image_mode="full", images=cache
    ).sync_full()

    assert again.image_calls == []
    assert report.images == 0


def test_download_thumbnails_fills_given_sources(tmp_path):
    cache = ImageCache(tmp_path)
    client = FakeClient([])
    progress: list[tuple[int, int, int, int]] = []

    saved, failed = download_thumbnails(
        client,
        cache,
        [(1, "https://img.example.com/1.jpg"), (2, "https://img.example.com/2.jpg")],
        on_progress=lambda *args: progress.append(args),
    )

    assert (saved, failed) == (2, 0)
    assert len(client.image_calls) == 2
    assert cache.has_thumb(1) and cache.has_thumb(2)
    assert progress[-1] == (2, 2, 2, 0)


def test_download_thumbnails_counts_failures(tmp_path):
    cache = ImageCache(tmp_path)
    client = FakeClient([], failing={thumb_url("https://img.example.com/1.jpg")})

    saved, failed = download_thumbnails(
        client, cache, [(1, "https://img.example.com/1.jpg")]
    )

    assert (saved, failed) == (0, 1)
    assert cache.has_thumb(1) is False
