from heibox.models import Page, Post
from heibox.sync import Syncer

USERID = 7
PER_PAGE = 20


def make_pages(count: int = 3) -> list[Page]:
    pages: list[Page] = []
    linkid = 1000
    timestamp = 1_700_000_000
    for _ in range(count):
        posts = []
        for _ in range(PER_PAGE):
            posts.append(Post(linkid=linkid, userid=USERID, create_at=timestamp))
            linkid += 1
            timestamp -= 3600
        pages.append(Page(tuple(posts), total_page=count, post_link_num=count * PER_PAGE))
    return pages


class FakeClient:
    def __init__(self, pages: list[Page]):
        self.pages = pages
        self.calls: list[int] = []

    def fetch_page(self, userid: int, offset: int, limit: int = 60) -> Page:
        self.calls.append(offset)
        index = offset // limit
        if index >= len(self.pages):
            return Page((), total_page=len(self.pages), post_link_num=len(self.pages) * PER_PAGE)
        return self.pages[index]


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
