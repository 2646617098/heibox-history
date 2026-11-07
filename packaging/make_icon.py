"""生成打包用的 app.ico（纯标准库，不依赖 Pillow）。

画一个"表盘"：深蓝圆底 + 白色表圈和指针 —— 呼应"历史/回顾"这个主题。
ICO 用 32 位 BMP 格式（Vista 及以后都认），文件只有几 KB。

用法：python packaging/make_icon.py
产物：packaging/app.ico
"""

from __future__ import annotations

import struct
from pathlib import Path

SIZE = 64
BACKGROUND = (36, 80, 126)
FOREGROUND = (245, 247, 250)


def _sample(x: int, y: int) -> tuple[int, int, int, int]:
    centre = (SIZE - 1) / 2
    dx, dy = x - centre, y - centre
    dist2 = dx * dx + dy * dy

    if dist2 > (SIZE * 0.47) ** 2:  # 圆外透明
        return (0, 0, 0, 0)

    ring_inner, ring_outer = SIZE * 0.30, SIZE * 0.355
    if ring_inner**2 <= dist2 <= ring_outer**2:  # 表圈
        return (*FOREGROUND, 255)

    if abs(dx) <= SIZE * 0.035 and -SIZE * 0.20 <= dy <= SIZE * 0.02:  # 分针
        return (*FOREGROUND, 255)
    if abs(dy) <= SIZE * 0.035 and 0 <= dx <= SIZE * 0.15:  # 时针
        return (*FOREGROUND, 255)

    return (*BACKGROUND, 255)


def build_ico(path: Path, size: int = SIZE) -> Path:
    pixels = bytearray()
    for y in range(size - 1, -1, -1):  # ICO 的位图是自下而上
        for x in range(size):
            red, green, blue, alpha = _sample(x, y)
            pixels += bytes((blue, green, red, alpha))

    mask_stride = ((size + 31) // 32) * 4
    mask = bytes(mask_stride * size)
    header = struct.pack(
        "<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(pixels) + len(mask), 0, 0, 0, 0
    )
    image = header + bytes(pixels) + mask
    entry = struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(image), 22)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<HHH", 0, 1, 1) + entry + image)
    return path


if __name__ == "__main__":
    target = Path(__file__).with_name("app.ico")
    print(build_ico(target), target.stat().st_size, "bytes")
