import json
from pathlib import Path

import httpx
import pytest
import respx

from heibox.api import HeyboxClient, LIST_URL
from heibox.errors import ApiError, ApiSchemaError, CaptchaRequired, NetworkError

FIXTURE = Path(__file__).parent / "fixtures" / "page_0.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@respx.mock
def test_parses_posts_and_builds_web_url():
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=load_fixture()))

    with HeyboxClient(interval=0, jitter=0) as client:
        page = client.fetch_page(20475763, 0)

    assert page.total_page == 14
    assert page.post_link_num == 797
    assert len(page.posts) == 2

    first = page.posts[0]
    assert first.linkid == 195926115
    assert first.create_at == 1793701596
    assert first.topics == ("边狱巴士公司",)
    assert first.url == "https://www.xiaoheihe.cn/app/bbs/link/06ee72c34b40"

    second = page.posts[1]
    assert second.url == "https://www.xiaoheihe.cn/app/bbs/link/195760904"
    # 老 fixture 里没有 link_award_num：保留 None，别伪造成 0 赞
    assert first.like_num is None


@respx.mock
def test_like_count_comes_from_link_award_num():
    fixture = load_fixture()
    fixture["post_links"][0]["link_award_num"] = 5
    fixture["post_links"][1]["link_award_num"] = 0
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=fixture))

    with HeyboxClient(interval=0, jitter=0) as client:
        page = client.fetch_page(20475763, 0)

    assert page.posts[0].like_num == 5
    assert page.posts[1].like_num == 0


@respx.mock
def test_sends_expected_query_and_caps_limit():
    route = respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=load_fixture()))

    with HeyboxClient(interval=0, jitter=0) as client:
        client.fetch_page(20475763, 120, limit=500)

    request = route.calls[0].request
    assert request.url.params["userid"] == "20475763"
    assert request.url.params["offset"] == "120"
    assert request.url.params["limit"] == "60"


@respx.mock
def test_failed_status_raises_api_error():
    respx.get(LIST_URL).mock(
        return_value=httpx.Response(200, json={"status": "failed", "msg": "invalid params"})
    )

    with HeyboxClient(interval=0, jitter=0) as client:
        with pytest.raises(ApiError, match="invalid params"):
            client.fetch_page(20475763, 0)


@respx.mock
def test_captcha_raises_dedicated_error():
    respx.get(LIST_URL).mock(
        return_value=httpx.Response(200, json={"status": "show_captcha", "msg": "请先验证"})
    )

    with HeyboxClient(interval=0, jitter=0) as client:
        with pytest.raises(CaptchaRequired):
            client.fetch_page(20475763, 300)


@respx.mock
def test_missing_post_links_raises_schema_error():
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json={"status": "ok"}))

    with HeyboxClient(interval=0, jitter=0) as client:
        with pytest.raises(ApiSchemaError):
            client.fetch_page(20475763, 0)


@respx.mock
def test_cover_urls_prefer_thumbs_and_fall_back_to_video_thumb():
    fixture = load_fixture()
    first, second = fixture["post_links"]
    first["thumbs"] = ["https://img.example.com/first-thumb.jpg?x=1"]
    first["imgs"] = ["https://img.example.com/first-full.jpg?x=1"]
    second["video_thumb"] = "https://img.example.com/second-video.jpg"

    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=fixture))
    with HeyboxClient(interval=0, jitter=0) as client:
        page = client.fetch_page(20475763, 0)

    assert page.posts[0].thumb_url == "https://img.example.com/first-thumb.jpg?x=1"
    assert page.posts[0].image_url == "https://img.example.com/first-full.jpg?x=1"
    # 视频帖没有 imgs/thumbs 时，video_thumb 同时作为缩略图和原图
    assert page.posts[1].thumb_url == "https://img.example.com/second-video.jpg"
    assert page.posts[1].image_url == "https://img.example.com/second-video.jpg"


@respx.mock
def test_posts_without_images_get_empty_urls():
    fixture = load_fixture()
    for item in fixture["post_links"]:
        item.pop("thumbs", None)
        item.pop("imgs", None)

    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=fixture))
    with HeyboxClient(interval=0, jitter=0) as client:
        page = client.fetch_page(20475763, 0)

    assert [post.thumb_url for post in page.posts] == ["", ""]
    assert [post.image_url for post in page.posts] == ["", ""]


@respx.mock
def test_fetch_image_returns_bytes_and_content_type():
    url = "https://img.example.com/thumb.png?imageView2/1/w/128/h/128/format/png"
    respx.get(url).mock(
        return_value=httpx.Response(
            200, content=b"\x89PNG\r\n\x1a\nrest", headers={"content-type": "image/png"}
        )
    )

    with HeyboxClient(interval=0, jitter=0) as client:
        data, content_type = client.fetch_image(url)

    assert data.startswith(b"\x89PNG")
    assert content_type == "image/png"


@respx.mock
def test_fetch_image_failure_raises_network_error():
    url = "https://img.example.com/broken.png"
    respx.get(url).mock(return_value=httpx.Response(500))

    with HeyboxClient(interval=0, jitter=0) as client:
        with pytest.raises(NetworkError):
            client.fetch_image(url)
