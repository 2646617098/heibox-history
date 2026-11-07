"""帖子封面缓存：只下载列表要显示的那一张小图。

接口返回的图片是 jpg/webp，而 tkinter 只认 PNG/GIF。为了不引入 Pillow
（这个项目的 EXE 体积对依赖很敏感），这里借图床自己的 imageView2 参数，
直接要一张 96×96 居中裁切的 PNG——正好是列表里 1:1 显示的尺寸，不用本地缩放。

目录结构（都在数据目录下，可以整个删掉重建）::

    images/thumb/<linkid>.png    列表缩略图，固定 96×96
    images/full/<linkid>.<ext>   可选的原图（每帖只存首图）
"""

from __future__ import annotations

import struct
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

THUMB_SIZE = 96
THUMB_QUERY = f"imageView2/1/w/{THUMB_SIZE}/h/{THUMB_SIZE}/format/png"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8"
GIF_MAGIC = b"GIF8"

CONTENT_SUFFIX = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def thumb_url(source: str) -> str:
    """把接口给的封面地址改写成 128×128 PNG；空地址返回空串。

    接口的 thumbs 本身带 ``?imageMogr2/...`` 参数，直接拼接会冲突，
    所以这里丢掉原查询串，只保留 scheme/host/path。
    """
    source = (source or "").strip()
    if not source:
        return ""
    if source.startswith("//"):
        source = "https:" + source
    parts = urlsplit(source)
    if not parts.scheme or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, THUMB_QUERY, ""))


def suffix_for(content_type: str, data: bytes = b"") -> str:
    """按 Content-Type 猜扩展名；猜不到再看文件头，最后退回 .img。"""
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype in CONTENT_SUFFIX:
        return CONTENT_SUFFIX[ctype]
    if data.startswith(PNG_MAGIC):
        return ".png"
    if data.startswith(JPEG_MAGIC):
        return ".jpg"
    if data.startswith(GIF_MAGIC):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".img"


def png_size(data: bytes) -> tuple[int, int] | None:
    """从 PNG 头里读宽高；不是 PNG 或头不完整时返回 None。"""
    if len(data) < 24 or not data.startswith(PNG_MAGIC) or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


class ImageCache:
    """本地图片目录。只负责落盘和路径，网络请求由调用方负责。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)

    # ---- 路径 -----------------------------------------------------

    @property
    def thumb_dir(self) -> Path:
        return self.data_dir / "images" / "thumb"

    @property
    def full_dir(self) -> Path:
        return self.data_dir / "images" / "full"

    def thumb_path(self, linkid: int) -> Path:
        return self.thumb_dir / f"{int(linkid)}.png"

    def full_path(self, linkid: int, suffix: str) -> Path:
        return self.full_dir / f"{int(linkid)}{suffix}"

    def resolve(self, relative: str) -> Path | None:
        """把库里存的相对路径还原成绝对路径；越界路径返回 None。"""
        relative = (relative or "").strip()
        if not relative:
            return None
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            return None
        return self.data_dir / path

    # ---- 缩略图 ---------------------------------------------------

    def has_thumb(self, linkid: int) -> bool:
        """文件存在且尺寸正确才算有图（旧版本 128px 的缓存会被自动重下）。"""
        path = self.thumb_path(linkid)
        try:
            with path.open("rb") as handle:
                head = handle.read(24)
        except OSError:
            return False
        return png_size(head) == (THUMB_SIZE, THUMB_SIZE)

    def save_thumb(self, linkid: int, data: bytes) -> bool:
        """只接受 96×96 的 PNG；先写临时文件再替换，避免留下半张图。"""
        if png_size(data) != (THUMB_SIZE, THUMB_SIZE):
            return False
        dest = self.thumb_path(linkid)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".png.part")
            tmp.write_bytes(data)
            tmp.replace(dest)
        except OSError:
            return False
        return True

    # ---- 原图 -----------------------------------------------------

    def save_full(self, linkid: int, data: bytes, content_type: str = "") -> str:
        """保存原图，返回相对数据目录的路径（写库用）；失败抛 OSError。"""
        suffix = suffix_for(content_type, data)
        dest = self.full_path(linkid, suffix)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return dest.relative_to(self.data_dir).as_posix()
