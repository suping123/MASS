import math, zlib, struct, os

PROJ = r"C:\Users\dell\WorkBuddy\2026-10-12-12-25-31\supply-chain-agent"
DESKTOP = r"C:\Users\dell\Desktop"
ICO_PATH = os.path.join(PROJ, "sca_icon.ico")
LNK_PATH = os.path.join(DESKTOP, "供应链多智能体系统.lnk")
TARGET = os.path.join(PROJ, "start_all.bat")


def smoothstep(e0, e1, x):
    if e1 == e0:
        return 0.0
    t = (x - e0) / (e1 - e0)
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def lerp(a, b, t):
    return a + (b - a) * t


def render(size):
    W = H = size
    cx = cy = size / 2.0
    R = size * 0.46
    ro = size * 0.185
    ri = size * 0.115
    ox1 = size * 0.40
    ox2 = size * 0.60
    oy = size * 0.5
    top = (0x2D, 0x7F, 0xF9)
    bot = (0x0B, 0x3D, 0x91)
    ring_col = (255, 255, 255)
    out = bytearray(W * H * 4)
    for y in range(H):
        for x in range(W):
            d = math.hypot(x + 0.5 - cx, y + 0.5 - cy)
            bg_a = 1.0 - smoothstep(R - 1.5, R + 1.5, d)
            d1 = math.hypot(x + 0.5 - ox1, y + 0.5 - oy)
            d2 = math.hypot(x + 0.5 - ox2, y + 0.5 - oy)
            ring1 = (1.0 - smoothstep(ro - 1.5, ro + 1.5, d1)) * smoothstep(ri - 1.5, ri + 1.5, d1)
            ring2 = (1.0 - smoothstep(ro - 1.5, ro + 1.5, d2)) * smoothstep(ri - 1.5, ri + 1.5, d2)
            ring = min(1.0, ring1 + ring2)
            a = max(bg_a, ring)
            i = (y * W + x) * 4
            if a <= 0.001:
                out[i:i + 4] = b'\x00\x00\x00\x00'
                continue
            if ring > 0.5:
                r, g, b = ring_col
                aa = ring
            else:
                t = y / size
                r = int(lerp(top[0], bot[0], t))
                g = int(lerp(top[1], bot[1], t))
                b = int(lerp(top[2], bot[2], t))
                aa = bg_a
            out[i] = r
            out[i + 1] = g
            out[i + 2] = b
            out[i + 3] = int(aa * 255)
    return bytes(out)


def encode_png(w, h, rgba):
    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]
    comp = zlib.compress(bytes(raw), 9)

    def chunk(typ, data):
        return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", comp)
    png += chunk(b"IEND", b"")
    return png


def make_ico(pngs):
    pngs = sorted(pngs, key=lambda p: -p[0])
    icondir = struct.pack("<HHH", 0, 1, len(pngs))
    entries = b''
    offset = 6 + 16 * len(pngs)
    for (w, h, data) in pngs:
        w8 = w & 0xFF if w < 256 else 0
        h8 = h & 0xFF if h < 256 else 0
        entries += struct.pack("<BBBBHHII", w8, h8, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    body = b''.join(d for (_, _, d) in pngs)
    return icondir + entries + body


def build_icon():
    pngs = [(256, 256, encode_png(256, 256, render(256))),
            (48, 48, encode_png(48, 48, render(48)))]
    with open(ICO_PATH, "wb") as f:
        f.write(make_ico(pngs))
    return ICO_PATH


def create_shortcut():
    import win32com.client
    sh = win32com.client.Dispatch("WScript.Shell")
    sc = sh.CreateShortcut(LNK_PATH)
    sc.TargetPath = TARGET
    sc.WorkingDirectory = PROJ
    sc.IconLocation = ICO_PATH + ",0"
    sc.Description = "供应链多智能体系统 - 一键启动 PostgreSQL + API"
    sc.WindowStyle = 1
    sc.Save()
    sc2 = sh.CreateShortcut(LNK_PATH)
    return {"target": sc2.TargetPath, "icon": sc2.IconLocation, "workdir": sc2.WorkingDirectory}


def main():
    ico = build_icon()
    print("ICO written:", ico, os.path.getsize(ico), "bytes")
    info = create_shortcut()
    print("LNK written:", LNK_PATH, os.path.exists(LNK_PATH))
    print("VERIFY TargetPath:", info["target"])
    print("VERIFY IconLocation:", info["icon"])
    print("VERIFY WorkingDirectory:", info["workdir"])


if __name__ == "__main__":
    main()
