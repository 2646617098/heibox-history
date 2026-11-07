import pytest

from heibox.errors import ConfigError
from heibox.models import Post
from heibox.store import Store


def test_upsert_is_idempotent(store, post_factory):
    first = post_factory(1001, 1_700_000_000, title="a")
    second = post_factory(1002, 1_700_000_100, title="b")

    assert store.upsert_posts([first, second]) == (2, 0)
    assert store.upsert_posts([second]) == (0, 1)
    assert store.count(7, 0, 10**10) == 2


def test_query_is_descending_and_inclusive(store, post_factory):
    store.upsert_posts(
        [
            post_factory(1, 100),
            post_factory(2, 200),
            post_factory(3, 300),
        ]
    )

    assert [post.linkid for post in store.query(7, 100, 300, 10)] == [3, 2, 1]
    assert [post.linkid for post in store.query(7, 200, 300, 10)] == [3, 2]
    assert [post.linkid for post in store.query(7, 0, 99, 10)] == []


def test_query_honours_limit_and_offset(store, post_factory):
    store.upsert_posts([post_factory(linkid, linkid * 100) for linkid in range(1, 6)])

    page_one = store.query(7, 0, 10**10, 2, 0)
    page_two = store.query(7, 0, 10**10, 2, 2)
    assert [post.linkid for post in page_one] == [5, 4]
    assert [post.linkid for post in page_two] == [3, 2]


def test_topics_round_trip(store, post_factory):
    store.upsert_posts([post_factory(1, 100, topics=("影之刃零", "动作"))])
    post = store.get_post(1)
    assert post is not None
    assert post.topics == ("影之刃零", "动作")


def test_store_is_bound_to_one_user(tmp_path):
    instance = Store(tmp_path / "one.db")
    instance.ensure_user(7)
    with pytest.raises(ConfigError):
        instance.ensure_user(8)
    instance.close()


def test_coverage_tracks_bounds_and_state(store, post_factory):
    store.upsert_posts([post_factory(1, 100), post_factory(2, 300)])

    coverage = store.coverage(7)
    assert coverage.total == 2
    assert coverage.oldest == 100
    assert coverage.newest == 300
    assert coverage.reached_end is False

    state = store.load_state(7)
    state.reached_end = True
    store.save_state(state)
    assert store.coverage(7).reached_end is True


def test_like_count_round_trip(store, post_factory):
    store.upsert_posts([post_factory(1, 100, like_num=5)])
    assert store.get_post(1).like_num == 5

    # 重新同步时更新为新值
    store.upsert_posts([post_factory(1, 100, like_num=9)])
    assert store.get_post(1).like_num == 9


def test_cover_urls_and_image_file_round_trip(store, post_factory):
    store.upsert_posts(
        [
            post_factory(
                1,
                100,
                thumb_url="https://img.example.com/t.jpg",
                image_url="https://img.example.com/f.jpg",
            )
        ]
    )

    post = store.get_post(1)
    assert post.thumb_url == "https://img.example.com/t.jpg"
    assert post.image_url == "https://img.example.com/f.jpg"
    assert post.image_file == ""

    store.set_image_file(1, "images/full/1.webp")
    assert store.get_post(1).image_file == "images/full/1.webp"
    assert store.image_files([1, 2]) == {1: "images/full/1.webp"}

    # 后续同步只更新接口字段，不能把本地原图路径覆盖掉
    store.upsert_posts([post_factory(1, 100, thumb_url="https://img.example.com/t2.jpg")])
    assert store.get_post(1).image_file == "images/full/1.webp"
    assert store.get_post(1).thumb_url == "https://img.example.com/t2.jpg"


def test_image_files_with_empty_input(store):
    assert store.image_files([]) == {}


def test_thumb_sources_and_missing_url_count(store, post_factory):
    store.upsert_posts(
        [
            post_factory(1, 100, thumb_url="https://img.example.com/1.jpg"),
            post_factory(2, 200),
            post_factory(3, 300, thumb_url="https://img.example.com/3.jpg"),
        ]
    )

    # 按时间倒序，方便补图时先补最近的
    assert store.thumb_sources(7) == [
        (3, "https://img.example.com/3.jpg"),
        (1, "https://img.example.com/1.jpg"),
    ]
    assert store.count_without_thumb_url(7) == 1
    assert store.count_without_thumb_url(8) == 0


def test_migrates_database_created_before_like_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE posts (
            linkid      INTEGER PRIMARY KEY,
            userid      INTEGER NOT NULL,
            create_at   INTEGER NOT NULL,
            title       TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            topics      TEXT NOT NULL DEFAULT '[]',
            url         TEXT NOT NULL DEFAULT '',
            link_type   INTEGER NOT NULL DEFAULT 0,
            click       INTEGER NOT NULL DEFAULT 0,
            comment_num INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO posts (linkid, userid, create_at, title) VALUES (1, 7, 100, 'old');
        """
    )
    legacy.commit()
    legacy.close()

    instance = Store(path)
    try:
        old = instance.get_post(1)
        assert old is not None
        assert old.like_num is None  # 老数据没有点赞信息
        assert old.thumb_url == ""
        assert old.image_url == ""
        assert old.image_file == ""

        instance.upsert_posts([Post(linkid=1, userid=7, create_at=100, like_num=3)])
        assert instance.get_post(1).like_num == 3
    finally:
        instance.close()
