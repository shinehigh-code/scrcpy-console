# -*- coding: utf-8 -*-
"""生成 scrcpy 控制台的应用图标（app.ico）：纯标准库，不依赖 PIL。

图形：深蓝圆角方块 + 白色手机机身 + 蓝色屏幕（投屏语义）。
用 4x4 超采样做抗锯齿，ICO 内嵌 PNG（Vista+ 支持）。
"""
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "app.ico"

BG = (37, 99, 235)      # #2563EB 产品蓝
BODY = (255, 255, 255)  # 手机机身
SCREEN = (37, 99, 235)  # 屏幕
SIZE = 256              # 基准画布


def inside_rr(px, py, x0, y0, x1, y1, r):
    """点是否落在圆角矩形内（含边界）。"""
    cx = min(max(px, x0 + r), x1 - r)
    cy = min(max(py, y0 + r), y1 - r)
    return (px - cx) ** 2 + (py - cy) ** 2 <= r * r


def coverage(x, y, box, r, ss=4):
    """单个输出像素被圆角矩形覆盖的比例（超采样抗锯齿）。"""
    x0, y0, x1, y1 = box
    hit = 0
    step = 1.0 / ss
    for i in range(ss):
        for j in range(ss):
            px = x + (i + 0.5) * step
            py = y + (j + 0.5) * step
            if inside_rr(px, py, x0, y0, x1, y1, r):
                hit += 1
    return hit / float(ss * ss)


def render(size):
    """渲染 size x size 的 RGBA 像素（返回 bytearray，行序自上而下）。"""
    k = size / float(SIZE)
    # 图层：(box, radius, rgba)，按顺序绘制
    layers = [
        ((8, 8, 248, 248), 52, BG + (255,)),        # 外底色
        ((84, 44, 172, 212), 14, BODY + (255,)),    # 手机机身
        ((94, 62, 162, 178), 6, SCREEN + (255,)),   # 屏幕
        ((112, 190, 144, 200), 7, BODY + (255,)),   # 机身底部留白（Home 区）
        ((108, 52, 148, 56), 2, SCREEN + (255,)),   # 听筒
    ]
    buf = bytearray()
    for y in range(size):
        for x in range(size):
            px, py = (x + 0.5) / k, (y + 0.5) / k
            r = g = b = a = 0
            for (bx, r_, col) in layers:
                box = (bx[0] * 1.0, bx[1] * 1.0, bx[2] * 1.0, bx[3] * 1.0)
                cov = coverage(px, py, box, r_)
                if cov <= 0:
                    continue
                # 源覆盖混合（alpha 预乘）
                inv = 1.0 - cov
                r = col[0] * cov + r * inv
                g = col[1] * cov + g * inv
                b = col[2] * cov + b * inv
                a = 255 * cov + a * inv
            buf += bytes((int(r), int(g), int(b), int(a)))
    return buf


def png(size, rgba):
    """把 RGBA 像素编码成 PNG（IHDR/IDAT/IEND，filter 0）。"""
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def build_ico(sizes=(16, 32, 48, 64, 128, 256)):
    images = []
    for s in sizes:
        images.append((s, png(s, render(s))))
    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    entries = bytearray()
    for s, data in images:
        entries += struct.pack("<BBBBHHII",
                               s if s < 256 else 0,
                               s if s < 256 else 0,
                               0, 0, 1, 32, len(data), offset)
        offset += len(data)
    out += entries
    for _, data in images:
        out += data
    return bytes(out)


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    ico = build_ico()
    OUT.write_bytes(ico)
    print("icon:", OUT, "%.1f KB" % (len(ico) / 1024))
