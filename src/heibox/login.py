"""扫码登录。

移植自 heibox-comment-bot-clean 的 auth_manager.login_with_qr + custom_signer：
1. GET /account/get_qrcode_url/ 拿二维码地址；
2. 轮询 GET /account/qr_state/，带上二维码地址里的参数；
3. result.error == "ok" 时收集 Set-Cookie，补 x_xhh_tokenid，拼成 Cookie 头。

签名算法（hkey / nonce / _time）也一并移植，仅用于登录接口。
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlparse

import httpx

from .errors import ApiError, ApiSchemaError, LoginError, NetworkError

BASE_API = "https://api.xiaoheihe.cn"
QR_URL_PATH = "/account/get_qrcode_url/"
QR_STATE_PATH = "/account/qr_state/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

# 与网页版登录请求保持一致
WEB_PARAMS = {
    "os_type": "web",
    "app": "web",
    "client_type": "web",
    "version": "999.0.4",
    "web_version": "2.5",
    "x_client_type": "web",
    "x_app": "heybox_website",
    "x_os_type": "Windows",
    "device_info": "Chrome",
}


@dataclass(frozen=True)
class SignKeys:
    hkey: str
    nonce: str
    time: int


class Signer:
    """小黑盒 web 端 hkey/nonce 生成器。"""

    _KEY = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"

    def get_keys(self, req_path: str) -> SignKeys:
        timestamp = int(time.time())
        nonce = self._nonce(timestamp)
        key = self._KEY

        part1 = self._av(str(timestamp), key, -2)
        part2 = self._sv(req_path, key)
        part3 = self._sv(nonce, key)

        parts = sorted([part1, part2, part3], key=len)
        mixed = self._interleave(parts)
        digest = hashlib.md5(mixed[:20].encode()).hexdigest()

        tail = [ord(char) for char in digest[-6:]]
        count = sum(self._mix(tail))
        prefix = self._av(digest[:5], key, -4)

        return SignKeys(hkey=f"{prefix}{count % 100:02d}", nonce=nonce, time=timestamp)

    @staticmethod
    def _nonce(timestamp: int) -> str:
        value = str(timestamp) + str(secrets.randbelow(timestamp * 1000))
        return hashlib.md5(value.encode()).hexdigest().upper()

    @staticmethod
    def _av(value: str, key: str, cut: int) -> str:
        alphabet = key[0 : len(key) + cut]
        return "".join(alphabet[ord(char) % len(alphabet)] for char in value)

    @staticmethod
    def _sv(value: str, key: str) -> str:
        return "".join(key[ord(char) % len(key)] for char in value)

    @staticmethod
    def _interleave(parts: list[str]) -> str:
        longest = parts[-1]
        out: list[str] = []
        for index in range(len(longest)):
            for part in parts:
                if len(part) > index:
                    out.append(part[index])
        return "".join(out)

    def _mix(self, values: list[int]) -> list[int]:
        result = [0] * 6
        result[0] = self._gm(values[0]) ^ self._ym(values[1]) ^ self._m(values[2]) ^ self._qm(values[3])
        result[1] = self._qm(values[0]) ^ self._gm(values[1]) ^ self._ym(values[2]) ^ self._m(values[3])
        result[2] = self._m(values[0]) ^ self._qm(values[1]) ^ self._gm(values[2]) ^ self._ym(values[3])
        result[3] = self._ym(values[0]) ^ self._m(values[1]) ^ self._qm(values[2]) ^ self._gm(values[3])
        result[4] = values[4]
        result[5] = values[5]
        return result

    @staticmethod
    def _vm(number: int) -> int:
        if number & 128:
            return ((number << 1) ^ 27) & 255
        return number << 1

    def _qm(self, number: int) -> int:
        return self._vm(number) ^ number

    def _m(self, number: int) -> int:
        return self._qm(self._vm(number))

    def _ym(self, number: int) -> int:
        return self._m(self._qm(self._vm(number)))

    def _gm(self, number: int) -> int:
        return self._ym(number) ^ self._m(number) ^ self._qm(number)


class QRLogin:
    def __init__(
        self,
        *,
        timeout: float = 180.0,
        interval: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        on_qr: Callable[[str], None] | None = None,
        on_state: Callable[[str, str], None] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.timeout = timeout
        self.interval = interval
        self._sleep = sleep
        self._on_qr = on_qr
        self._on_state = on_state
        self.signer = Signer()
        self._client = httpx.Client(
            timeout=20.0,
            follow_redirects=True,
            transport=transport,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": "https://www.xiaoheihe.cn/",
                "Origin": "https://www.xiaoheihe.cn",
                "Accept": "application/json, text/plain, */*",
            },
        )

    def __enter__(self) -> "QRLogin":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def run(self) -> tuple[str, str]:
        """返回 (Cookie 头, 昵称)。"""
        payload = self._get(QR_URL_PATH)
        result = payload.get("result")
        qr_url = str((result or {}).get("qr_url") or "").strip() if isinstance(result, dict) else ""
        if not qr_url:
            raise LoginError(f"没有拿到二维码地址：{payload.get('msg') or payload}")

        if self._on_qr is not None:
            self._on_qr(qr_url)

        query = dict(parse_qsl(urlparse(qr_url).query, keep_blank_values=True))
        if not query:
            raise LoginError("二维码地址里没有可轮询的参数")

        deadline = time.monotonic() + max(10.0, self.timeout)
        last_state = ""
        while time.monotonic() < deadline:
            state_payload = self._get(QR_STATE_PATH, extra=query)
            state = state_payload.get("result")
            state = state if isinstance(state, dict) else {}
            error = str(state.get("error") or "")
            message = str(state.get("error_msg") or "")
            nickname = str(state.get("nickname") or "")

            if error != last_state and self._on_state is not None:
                self._on_state(error, message)
            last_state = error

            if error == "ok":
                cookies = dict(self._client.cookies.items())
                if not cookies:
                    raise LoginError("登录成功但没有收到 Cookie")
                cookies.setdefault("x_xhh_tokenid", self._tokenid())
                header = "; ".join(f"{name}={value}" for name, value in cookies.items())
                return header, nickname

            if error.lower() in {"expired", "timeout", "cancel"}:
                raise LoginError(f"二维码登录已终止：{error} {message}".strip())

            self._sleep(self.interval)

        raise LoginError(f"二维码登录超时（{self.timeout:.0f} 秒）")

    def _get(self, path: str, extra: dict[str, str] | None = None) -> dict:
        keys = self.signer.get_keys(path)
        params: dict[str, str] = dict(WEB_PARAMS)
        params.update(
            {
                "hkey": keys.hkey,
                "nonce": keys.nonce,
                "_time": str(keys.time),
                "_notip": "true",
            }
        )
        if extra:
            params.update(extra)

        try:
            response = self._client.get(BASE_API + path, params=params)
        except httpx.HTTPError as exc:
            raise NetworkError(f"登录请求失败：{exc}") from exc

        if response.status_code >= 400:
            raise ApiError(f"登录接口 HTTP {response.status_code}：{response.text[:120]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ApiSchemaError("登录接口返回的不是 JSON，可能被风控") from exc
        if not isinstance(payload, dict):
            raise ApiSchemaError("登录接口响应结构异常")

        status = payload.get("status")
        if status in {"show_captcha", "login"}:
            raise LoginError(f"登录接口要求验证：{payload.get('msg') or status}")
        if status not in {None, "", "ok"}:
            raise ApiError(f"登录接口返回失败：{payload.get('msg') or status}")
        return payload

    @staticmethod
    def _tokenid() -> str:
        raw = bytearray()
        for seed in (str(int(time.time())), "asda", "sdaasf", "sadasdas"):
            raw.extend(hashlib.md5(seed.encode()).digest())
        raw.append(0)
        return base64.b64encode(bytes(raw)).decode("ascii")


def render_qr(console, qr_url: str, png_path: Path) -> Path | None:
    """终端里画二维码，并在有 Pillow 时保存 PNG；返回实际保存的路径。"""
    try:
        import qrcode
    except ImportError:
        console.print("[yellow]未安装 qrcode，无法显示二维码；请先 pip install qrcode[/yellow]")
        console.print(qr_url)
        return None

    code = qrcode.QRCode(border=1)
    code.add_data(qr_url)
    code.make(fit=True)
    picture = "\n".join(
        "".join("██" if cell else "  " for cell in row) for row in code.get_matrix()
    )
    console.print(picture, highlight=False, soft_wrap=True)

    try:
        image = code.make_image(fill_color="black", back_color="white")
        png_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(png_path)
        return png_path
    except Exception:  # noqa: BLE001 - 没有 Pillow 时只是不存图片
        return None
