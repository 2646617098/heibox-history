import pytest

from heibox.config import (
    Config,
    as_bool,
    load_config,
    normalize_cookie,
    normalize_image_mode,
    save_config,
)
from heibox.errors import ConfigError


def test_config_round_trip(tmp_path):
    save_config(tmp_path, Config(userid=42, request_interval=2.5))
    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.userid == 42
    assert loaded.request_interval == 2.5
    assert loaded.cookie == ""
    assert loaded.image_mode == "off"
    assert loaded.show_thumbs is True


def test_show_thumbs_round_trip(tmp_path):
    save_config(tmp_path, Config(userid=42, show_thumbs=False))
    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.show_thumbs is False


def test_image_mode_round_trip(tmp_path):
    save_config(tmp_path, Config(userid=42, image_mode="thumb"))
    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.image_mode == "thumb"


def test_unknown_image_mode_falls_back_to_off(tmp_path):
    (tmp_path / "config.toml").write_text(
        'userid = 42\nimage_mode = "everything"\n', encoding="utf-8"
    )
    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.image_mode == "off"


def test_normalize_image_mode():
    assert normalize_image_mode("THUMB") == "thumb"
    assert normalize_image_mode(" full ") == "full"
    assert normalize_image_mode("") == "off"
    assert normalize_image_mode(None) == "off"


def test_as_bool_accepts_toml_and_hand_written_values():
    assert as_bool(True) is True
    assert as_bool(False) is False
    assert as_bool("false") is False
    assert as_bool("0") is False
    assert as_bool("off") is False
    assert as_bool("yes") is True
    assert as_bool(None, False) is False


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
