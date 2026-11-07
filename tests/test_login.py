import io
import re

import httpx
import pytest
import respx
from rich.console import Console

from heibox.errors import LoginError
from heibox.login import (
    BASE_API,
    QR_STATE_PATH,
    QR_URL_PATH,
    QRLogin,
    Signer,
    extract_userid,
    render_qr,
)


def route(path: str):
    return respx.get(url__regex=rf"^{re.escape(BASE_API + path)}\?")


def test_signer_output_shape():
    keys = Signer().get_keys(QR_STATE_PATH)

    assert len(keys.nonce) == 32
    assert keys.nonce == keys.nonce.upper()
    assert len(keys.hkey) == 7
    assert keys.hkey[-2:].isdigit()
    assert keys.time > 0


def test_extract_userid_prefers_user_heybox_id():
    cookie = "user_pkey=x; user_heybox_id=20475763; heybox_id=999"
    assert extract_userid(cookie) == 20475763


def test_extract_userid_falls_back_and_tolerates_junk():
    assert extract_userid("heybox_id=123; foo=bar") == 123
    assert extract_userid("user_heybox_id=abc; heybox_id=456") == 456
    assert extract_userid("user_pkey=x") is None


def test_render_qr_prints_terminal_blocks(tmp_path):
    console = Console(file=io.StringIO(), width=100)
    render_qr(console, "https://example.com/account/qr?x=1", tmp_path / "qr.png")

    assert "██" in console.file.getvalue()


@respx.mock
def test_qr_login_collects_cookies_and_tokenid():
    route(QR_URL_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "result": {"qr_url": "https://www.xiaoheihe.cn/account/qr?qr_code=abc123"},
            },
        )
    )
    route(QR_STATE_PATH).mock(
        side_effect=[
            httpx.Response(200, json={"status": "ok", "result": {"error": "waiting"}}),
            httpx.Response(
                200,
                json={"status": "ok", "result": {"error": "ok", "nickname": "tester"}},
                headers={"set-cookie": "user_pkey=xyz; Path=/; Domain=.xiaoheihe.cn"},
            ),
        ]
    )

    with QRLogin(timeout=5, interval=0, sleep=lambda _: None) as qr:
        cookie, nickname = qr.run()

    assert nickname == "tester"
    assert "user_pkey=xyz" in cookie
    assert "x_xhh_tokenid=" in cookie


@respx.mock
def test_qr_login_expired_raises():
    route(QR_URL_PATH).mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "result": {"qr_url": "https://x/account/qr?qr_code=abc"}}
        )
    )
    route(QR_STATE_PATH).mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "result": {"error": "expired", "error_msg": "二维码已过期"}}
        )
    )

    with QRLogin(timeout=5, interval=0, sleep=lambda _: None) as qr:
        with pytest.raises(LoginError, match="过期"):
            qr.run()


@respx.mock
def test_qr_login_rejects_captcha():
    route(QR_URL_PATH).mock(
        return_value=httpx.Response(200, json={"status": "show_captcha", "msg": "请先验证"})
    )

    with QRLogin(timeout=5, interval=0, sleep=lambda _: None) as qr:
        with pytest.raises(LoginError):
            qr.run()
