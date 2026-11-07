"""测试用：生成真正的 PNG（Tk 和尺寸校验都要求文件头合法）。"""

from __future__ import annotations

import struct
import zlib


def make_png(width: int = 96, height: int = 96, rgb: tuple[int, int, int] = (180, 180, 190)) -> bytes:
    row = b"\x00" + bytes(rgb) * width
    raw = row * height

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
