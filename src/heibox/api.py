"""小黑盒帖子列表接口封装：限速、重试、字段映射。

实测要点（2026-11）：
- 路径 /bbs/app/profile/user/link/list，参数是 userid（不是 user_id）；
- limit 上限 60，超过按 60 处理；
- offset 按“原始条目”计数，单页返回条数可能少于 limit；
- 目前无需登录态与签名，但仍按“随时会变”来设计。
"""

from __future__ import annotations

import random
import time as _time
from urllib.parse import parse_qs, urlparse

import httpx

from .errors import ApiError, ApiSchemaError, CaptchaRequired, NetworkError
from .models import Page, Post

LIST_URL = "https://api.xiaoheihe.cn/bbs/app/profile/user/link/list"
WEB_LINK = "https://www.xiaoheihe.cn/app/bbs/link/{id}"
DEFAULT_LIMIT = 60
MAX_LIMIT = 60
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class HeyboxClient:
    def __init__(
        self,
        *,
        interval: float = 3.0,
        jitter: float = 1.0,
        timeout: float = 15.0,
        retries: int = 3,
        backoff: tuple[float, ...] = (5.0, 15.0, 45.0),
        sleep=_time.sleep,
        transport: httpx.BaseTransport | None = None,
        cookie: str = "",
    ) -> None:
        self.interval = interval
        self.jitter = jitter
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep
        self._last = 0.0
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.xiaoheihe.cn/",
            "Accept": "application/json, text/plain, */*",
        }
        if cookie:
            headers["Cookie"] = cookie
        self._client = httpx.Client(timeout=timeout, headers=headers, transport=transport)

    def __enter__(self) -> "HeyboxClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _throttle(self) -> None:
        if self.interval <= 0:
            self._last = _time.monotonic()
            return
        wait = self.interval - (_time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait + random.uniform(0, self.jitter))
        self._last = _time.monotonic()

    def fetch_page(self, userid: int, offset: int, limit: int = DEFAULT_LIMIT) -> Page:
        if not 0 < limit <= MAX_LIMIT:
            limit = MAX_LIMIT
        params = {"userid": userid, "offset": offset, "limit": limit}
        last_error: NetworkError | None = None

        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                response = self._client.get(LIST_URL, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = NetworkError(f"网络错误：{exc}")
            else:
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    last_error = NetworkError(f"HTTP {response.status_code}，稍后重试")
                elif response.status_code >= 400:
                    raise ApiError(f"HTTP {response.status_code}：{response.text[:120]}")
                else:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise ApiSchemaError("响应不是合法 JSON，可能被风控或接口已改版") from exc
                    return self._parse(payload)

            if attempt < self.retries:
                delay = self.backoff[min(attempt, len(self.backoff) - 1)]
                if delay > 0:
                    self._sleep(delay)

        raise last_error or NetworkError("请求失败")

    def _parse(self, payload: object) -> Page:
        if not isinstance(payload, dict):
            raise ApiSchemaError("响应结构变化：预期 JSON 对象")

        status = payload.get("status")
        if status == "show_captcha":
            raise CaptchaRequired(payload.get("msg") or "请先验证")
        if status != "ok":
            message = payload.get("msg") or status or "未知错误"
            raise ApiError(f"接口返回失败：{message}")

        raw_posts = payload.get("post_links")
        if not isinstance(raw_posts, list):
            raise ApiSchemaError("响应结构变化：缺少 post_links 列表，请升级 heibox")

        posts = tuple(self._to_post(item) for item in raw_posts if isinstance(item, dict))
        user = payload.get("user")
        if not isinstance(user, dict):
            user = {}

        return Page(
            posts=posts,
            total_page=_as_int(payload.get("total_page")),
            post_link_num=_as_int(user.get("post_link_num")),
            profile_userid=_as_int(user.get("userid")),
        )

    def _to_post(self, item: dict) -> Post:
        linkid = _as_int(item.get("linkid"))
        if linkid <= 0:
            raise ApiSchemaError("响应结构变化：帖子缺少 linkid")

        topics = tuple(
            str(topic["name"])
            for topic in item.get("topics") or []
            if isinstance(topic, dict) and topic.get("name")
        )

        return Post(
            linkid=linkid,
            userid=_as_int(item.get("userid")),
            create_at=_as_int(item.get("create_at")),
            title=str(item.get("title") or "").strip(),
            description=str(item.get("description") or "").strip(),
            topics=topics,
            url=_web_url(item, linkid),
            link_type=_as_int(item.get("link_type")),
            click=_as_int(item.get("click")),
            comment_num=_as_int(item.get("comment_num")),
            like_num=_opt_int(item.get("link_award_num")),
        )


def _web_url(item: dict, linkid: int) -> str:
    """优先用 share_url 里的 hex link_id 拼网页地址，失败时退回数字 id。"""
    share = item.get("share_url")
    if isinstance(share, str) and share:
        query = parse_qs(urlparse(share).query)
        hex_id = (query.get("link_id") or [""])[0]
        if hex_id:
            return WEB_LINK.format(id=hex_id)
    return WEB_LINK.format(id=linkid)


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _opt_int(value: object) -> int | None:
    """字段缺失时保留 None（未知），不要伪造成 0。"""
    if value is None:
        return None
    return _as_int(value)
