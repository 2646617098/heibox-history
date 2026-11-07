from pathlib import Path

from heibox.images import THUMB_SIZE, ImageCache, png_size, suffix_for, thumb_url
from pngutil import make_png

PNG = make_png(THUMB_SIZE, THUMB_SIZE)


def test_thumb_url_replaces_existing_query():
    source = "https://img.example.com/a/b/thumb.jpeg?imageMogr2/format/webp"

    assert thumb_url(source) == (
        "https://img.example.com/a/b/thumb.jpeg?"
        f"imageView2/1/w/{THUMB_SIZE}/h/{THUMB_SIZE}/format/png"
    )
    assert thumb_url("") == ""
    assert thumb_url("   ") == ""


def test_thumb_url_accepts_protocol_relative_address():
    assert thumb_url("//img.example.com/a.jpg").startswith(
        "https://img.example.com/a.jpg?"
    )


def test_thumb_url_rejects_non_http_input():
    assert thumb_url("data:image/png;base64,xxx") == ""


def test_suffix_for_prefers_content_type_then_magic():
    assert suffix_for("image/jpeg") == ".jpg"
    assert suffix_for("image/webp; charset=binary") == ".webp"
    assert suffix_for("", PNG) == ".png"
    assert suffix_for("", b"\xff\xd8\xff\xe0") == ".jpg"
    assert suffix_for("", b"\x00\x00") == ".img"


def test_png_size_reads_ihdr():
    assert png_size(PNG) == (THUMB_SIZE, THUMB_SIZE)
    assert png_size(make_png(128, 96)) == (128, 96)
    assert png_size(b"not a png") is None


def test_save_thumb_rejects_wrong_format_and_size(tmp_path):
    cache = ImageCache(tmp_path)

    assert cache.save_thumb(1, b"<html>not an image</html>") is False
    assert cache.save_thumb(1, make_png(128, 128)) is False
    assert cache.has_thumb(1) is False

    assert cache.save_thumb(1, PNG) is True
    assert cache.has_thumb(1) is True
    assert cache.thumb_path(1).read_bytes() == PNG


def test_has_thumb_rejects_old_size_cache(tmp_path):
    cache = ImageCache(tmp_path)
    path = cache.thumb_path(1)
    path.parent.mkdir(parents=True)
    path.write_bytes(make_png(128, 128))

    assert cache.has_thumb(1) is False


def test_save_full_returns_relative_path(tmp_path):
    cache = ImageCache(tmp_path)
    relative = cache.save_full(2, b"\xff\xd8jpeg", "image/jpeg")

    assert relative == "images/full/2.jpg"
    assert (tmp_path / relative).read_bytes() == b"\xff\xd8jpeg"
    assert cache.resolve(relative) == tmp_path / relative


def test_resolve_rejects_escaping_paths(tmp_path):
    cache = ImageCache(tmp_path)
    assert cache.resolve("") is None
    assert cache.resolve("../secret.txt") is None
    assert cache.resolve(str(tmp_path / "x")) is None


def test_data_dir_is_not_shared_by_two_caches(tmp_path):
    first = ImageCache(tmp_path / "one")
    second = ImageCache(tmp_path / "two")
    first.save_thumb(1, PNG)

    assert first.has_thumb(1) is True
    assert second.has_thumb(1) is False
    assert Path(first.thumb_path(1)).is_relative_to(tmp_path)
