import pytest

from heibox.config import Config, load_config, normalize_cookie, save_config
from heibox.errors import ConfigError


def test_config_round_trip(tmp_path):
    save_config(tmp_path, Config(userid=42, request_interval=2.5))
    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.userid == 42
    assert loaded.request_interval == 2.5
    assert loaded.cookie == ""


def test_config_accepts_utf8_bom(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes("\ufeffuserid = 42\nrequest_interval = 2.5\n".encode("utf-8"))
    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.userid == 42


def test_config_cookie_round_trip(tmp_path):
    save_config(tmp_path, Config(userid=42, cookie='a=1; b="2"'))
    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.cookie == 'a=1; b="2"'


def test_normalize_cookie_variants():
    assert normalize_cookie("a=1; b=2") == "a=1; b=2"
    assert normalize_cookie("Cookie: a=1;  b=2") == "a=1; b=2"
    assert normalize_cookie('"a=1; b=2"') == "a=1; b=2"
    assert normalize_cookie("  ") == ""


def test_broken_config_raises_readable_error(tmp_path):
    (tmp_path / "config.toml").write_text("not toml at all", encoding="utf-8")
    with pytest.raises(ConfigError, match="配置文件无法解析"):
        load_config(tmp_path)
