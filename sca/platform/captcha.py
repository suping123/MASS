"""图形验证码：纯标准库生成 PNG，不引入 Pillow。

为什么不直接返回文本验证码：文本下发等于把答案交给调用方，
"人机校验"退化成一次多余的接口调用。这里改为把答案画成图片，
只有能看图的人（或使用服务的用户）才能读出来，答案只存在服务端 Redis。

实现要点：
- 5x7 点阵字模放大绘制，逐字随机基线偏移 + 随机深色，机器切分和模板匹配都要多花成本；
- 整图做一次正弦波浪位移 + 干扰线 + 噪点，破坏规则网格；
- 字符集剔除 0/O/1/I/L 等易混形状，减少"看不清所以反复刷新"的挫败感；
- PNG 手写编码（IHDR/IDAT/IEND + zlib），零第三方依赖。
"""
from __future__ import annotations

import base64
import math
import random
import struct
import zlib
from typing import Dict, List, Tuple

# 剔除易混字符：0 O 1 I L
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LEN = 4

WIDTH = 150
HEIGHT = 58
_SCALE = 5          # 每个点阵像素放大到 5x5（笔画够粗，缩小显示也认得出）
_LEFT = 14
_GAP = 3

# 5x7 点阵，每行 5 bit（1 = 点）。只保留 ALPHABET 里出现的字符。
_FONT: Dict[str, Tuple[int, ...]] = {
    "2": (0b01110, 0b10001, 0b00001, 0b00010, 0b00100, 0b01000, 0b11111),
    "3": (0b11111, 0b00010, 0b00100, 0b00010, 0b00001, 0b10001, 0b01110),
    "4": (0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010),
    "5": (0b11111, 0b10000, 0b11110, 0b00001, 0b00001, 0b10001, 0b01110),
    "6": (0b00110, 0b01000, 0b10000, 0b11110, 0b10001, 0b10001, 0b01110),
    "7": (0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000),
    "8": (0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110),
    "9": (0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00010, 0b01100),
    "A": (0b01110, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001),
    "B": (0b11110, 0b10001, 0b10001, 0b11110, 0b10001, 0b10001, 0b11110),
    "C": (0b01110, 0b10001, 0b10000, 0b10000, 0b10000, 0b10001, 0b01110),
    "D": (0b11110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b11110),
    "E": (0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b11111),
    "F": (0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b10000),
    "G": (0b01110, 0b10001, 0b10000, 0b10111, 0b10001, 0b10001, 0b01111),
    "H": (0b10001, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001),
    "J": (0b00111, 0b00010, 0b00010, 0b00010, 0b00010, 0b10010, 0b01100),
    "K": (0b10001, 0b10010, 0b10100, 0b11000, 0b10100, 0b10010, 0b10001),
    "M": (0b10001, 0b11011, 0b10101, 0b10101, 0b10001, 0b10001, 0b10001),
    "N": (0b10001, 0b11001, 0b10101, 0b10011, 0b10001, 0b10001, 0b10001),
    "P": (0b11110, 0b10001, 0b10001, 0b11110, 0b10000, 0b10000, 0b10000),
    "Q": (0b01110, 0b10001, 0b10001, 0b10001, 0b10101, 0b10010, 0b01101),
    "R": (0b11110, 0b10001, 0b10001, 0b11110, 0b10100, 0b10010, 0b10001),
    "S": (0b01111, 0b10000, 0b10000, 0b01110, 0b00001, 0b00001, 0b11110),
    "T": (0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100),
    "U": (0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110),
    "V": (0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01010, 0b00100),
    "W": (0b10001, 0b10001, 0b10001, 0b10101, 0b10101, 0b11011, 0b10001),
    "X": (0b10001, 0b10001, 0b01010, 0b00100, 0b01010, 0b10001, 0b10001),
    "Y": (0b10001, 0b10001, 0b01010, 0b00100, 0b00100, 0b00100, 0b00100),
    "Z": (0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b10000, 0b11111),
}

# 深色墨迹候选，保证与浅背景对比度足够（亮度都压得很低）
_INK = [(24, 42, 74), (17, 61, 52), (58, 30, 70), (72, 38, 20),
        (20, 52, 88), (40, 48, 24), (66, 22, 44)]


def new_code(length: int = CODE_LEN) -> str:
    return "".join(random.choice(ALPHABET) for _ in range(length))


def _png(width: int, height: int, buf: bytearray) -> bytes:
    """手写 PNG：8bit 真彩色（color type 2），每行前置 filter=0。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw += buf[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def _set(buf: bytearray, x: int, y: int, color: Tuple[int, int, int]) -> None:
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        i = (y * WIDTH + x) * 3
        buf[i], buf[i + 1], buf[i + 2] = color


def _blend(buf: bytearray, x: int, y: int, color: Tuple[int, int, int], a: float) -> None:
    if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
        return
    i = (y * WIDTH + x) * 3
    for k in range(3):
        buf[i + k] = int(buf[i + k] * (1 - a) + color[k] * a)


def render(code: str) -> bytes:
    """把验证码画成 PNG 字节流。"""
    buf = bytearray(WIDTH * HEIGHT * 3)

    # 背景：纵向浅灰蓝渐变
    for y in range(HEIGHT):
        t = y / max(HEIGHT - 1, 1)
        top = (248, 250, 253)
        bot = (226, 234, 244)
        row = tuple(int(top[k] + (bot[k] - top[k]) * t) for k in range(3))
        for x in range(WIDTH):
            _set(buf, x, y, row)  # type: ignore[arg-type]

    # 字符：逐字随机基线偏移，模拟手持印刷的歪斜
    step = 5 * _SCALE + _GAP
    for idx, ch in enumerate(code):
        rows = _FONT.get(ch)
        if not rows:
            continue
        ink = random.choice(_INK)
        base_x = _LEFT + idx * step + random.randint(-1, 1)
        base_y = (HEIGHT - 7 * _SCALE) // 2 + random.randint(-2, 2)
        for ry, bits in enumerate(rows):
            for rx in range(5):
                if not (bits >> (4 - rx)) & 1:
                    continue
                for dy in range(_SCALE):
                    for dx in range(_SCALE):
                        # 边缘半透明，字看起来不那么"像素块"
                        a = 1.0
                        if dx in (0, _SCALE - 1) or dy in (0, _SCALE - 1):
                            a = 0.72
                        _blend(buf, base_x + rx * _SCALE + dx,
                               base_y + ry * _SCALE + dy, ink, a)

    # 干扰线：两条窄正弦曲线，穿过字符但颜色浅
    for _ in range(2):
        color = (172 + random.randint(0, 34),) * 3
        y0 = random.randint(8, HEIGHT - 8)
        amp = random.uniform(3, 7)
        freq = random.uniform(0.045, 0.09)
        phase = random.uniform(0, 6.28)
        for x in range(WIDTH):
            y = int(y0 + amp * math.sin(freq * x + phase))
            _blend(buf, x, y, color, 0.55)
            _blend(buf, x, y + 1, color, 0.3)

    # 噪点
    for _ in range(int(WIDTH * HEIGHT * 0.012)):
        x, y = random.randrange(WIDTH), random.randrange(HEIGHT)
        _blend(buf, x, y, (90, 100, 120), random.uniform(0.08, 0.25))

    # 整行波浪位移：破坏字符的整齐基线，剪切变换还能保持笔画连通
    out = bytearray(WIDTH * HEIGHT * 3)
    for y in range(HEIGHT):
        shift = int(round(1.6 * math.sin(y * 0.28)))
        for x in range(WIDTH):
            sx = min(max(x - shift, 0), WIDTH - 1)
            si = (y * WIDTH + sx) * 3
            di = (y * WIDTH + x) * 3
            out[di:di + 3] = buf[si:si + 3]
    return _png(WIDTH, HEIGHT, out)


def data_uri(code: str) -> str:
    """直接内联的 data URI：前端一个 <img src> 就能显示，不用额外请求。"""
    return "data:image/png;base64," + base64.b64encode(render(code)).decode("ascii")


def normalize(raw: str) -> str:
    """用户输入归一：去空格、转大写（图片里只有大写字母和数字）。"""
    return "".join(ch for ch in str(raw or "").upper() if ch.isalnum())


def preview(code: str) -> List[str]:
    """给自检脚本用：返回字形覆盖情况，便于断言字模没有漏字符。"""
    return [ch for ch in code if ch in _FONT]
