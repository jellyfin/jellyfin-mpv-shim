"""Generate a tiny synthetic VobSub (.idx/.sub): 2-colour text bitmaps as DVD SPU packets."""
import struct, sys
from PIL import Image, ImageDraw, ImageFont
W, H = 720, 576
CUES = [(2000, 6000, "SYNTHETIC VOBSUB ONE"), (8000, 12000, "SYNTHETIC VOBSUB TWO"), (14000, 18000, "SYNTHETIC VOBSUB THREE")]
font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 30)

def runs(row):
    out = []; i = 0
    while i < len(row):
        j = i
        while j < len(row) and row[j] == row[i] and j - i < 255: j += 1
        out.append((j - i, row[i])); i = j
    return out

def enc_line(row):
    nib = []
    rs = runs(row)
    if rs[-1][1] == 0: rs = rs[:-1]; tail = True   # trailing bg run -> "to end of line"
    else: tail = False
    for ln, c in rs:
        v = (ln << 2) | c
        n = 1 if ln <= 3 else 2 if ln <= 15 else 3 if ln <= 63 else 4
        nib += [(v >> (4 * k)) & 0xF for k in range(n - 1, -1, -1)]
    if tail: nib += [0, 0, 0, 0]
    if len(nib) % 2: nib.append(0)
    return bytes((nib[i] << 4) | nib[i + 1] for i in range(0, len(nib), 2))

def spu(text, dur_ms):
    img = Image.new("L", (440, 44), 0); d = ImageDraw.Draw(img)
    d.text((8, 2), text, fill=255, font=font)
    px = img.point(lambda v: 1 if v > 100 else 0)
    w, h = px.size; h -= h % 2
    rows = [[px.getpixel((x, y)) for x in range(w)] for y in range(h)]
    top = b"".join(enc_line(r) for r in rows[0::2]); bot = b"".join(enc_line(r) for r in rows[1::2])
    x1, y1 = 140, 480; x2, y2 = x1 + w - 1, y1 + h - 1
    ctrl_off = 4 + len(top) + len(bot)
    c1_len = 2 + 2 + 1 + 3 + 3 + 7 + 5 + 1   # date,next,01,03,04,05,06,ff
    c2_off = ctrl_off + c1_len
    date_end = int(dur_ms * 90 / 1024)
    c1 = struct.pack(">HH", 0, c2_off) + b"\x01" + b"\x03\x10\x00"[:0] + b"\x03" + bytes([0x00, 0x10]) \
        + b"\x04" + bytes([0x0F, 0xF0 & 0xF0 | 0x00])[:2] \
        + b"\x05" + bytes([x1 >> 4, ((x1 & 15) << 4) | (x2 >> 8), x2 & 255, y1 >> 4, ((y1 & 15) << 4) | (y2 >> 8), y2 & 255]) \
        + b"\x06" + struct.pack(">HH", 4, 4 + len(top)) + b"\xff"
    c2 = struct.pack(">HH", date_end, c2_off) + b"\x02\xff"
    body = top + bot
    pk = struct.pack(">HH", 4 + len(body) + len(c1) + len(c2), ctrl_off) + body + c1 + c2
    assert len(c1) == c1_len, (len(c1), c1_len)
    return pk

def scr(t90):  # SCR for pack header
    b = (1 << 46) | ((t90 >> 30) & 7) << 43 | 1 << 42 | ((t90 >> 15) & 0x7FFF) << 27 | 1 << 26 | (t90 & 0x7FFF) << 11 | 1 << 10 | 1 << 9
    return struct.pack(">Q", b)[2:] + bytes([0x01, 0x89, 0xC3, 0xF8])[0:0]

def pack(pk, pts_ms):
    t = pts_ms * 90
    sc = ((0b01 << 44) | (((t >> 30) & 7) << 41) | (1 << 40) | (((t >> 15) & 0x7FFF) << 25) | (1 << 24) | ((t & 0x7FFF) << 9) | (1 << 8) | (1 << 7))
    head = b"\x00\x00\x01\xba" + sc.to_bytes(6, "big") + bytes([0x01, 0x89, 0xC3, 0xF8])[:3]
    head = b"\x00\x00\x01\xba" + sc.to_bytes(6, "big") + b"\x01\x89\xc3" + b"\xf8"
    pts = bytes([0x21 | (((t >> 30) & 7) << 1), (t >> 22) & 255, 0x01 | (((t >> 15) & 127) << 1), (t >> 7) & 255, 0x01 | ((t & 127) << 1)])
    payload = b"\x20" + pk
    pes_data = b"\x81\x80\x05" + pts + payload
    return head + b"\x00\x00\x01\xbd" + struct.pack(">H", len(pes_data)) + pes_data

sub = b""; idx = ["# VobSub index file, v7 (do not modify this line!)", "size: %dx%d" % (W, H),
    "palette: 000000, ffffff, 808080, 000000, 000000, 000000, 000000, 000000, 000000, 000000, 000000, 000000, 000000, 000000, 000000, 000000",
    "langidx: 0", "", "id: en, index: 0"]
for s, e, t in CUES:
    p = pack(spu(t, e - s), s)
    idx.append("timestamp: 00:00:%02d:%03d, filepos: %09x" % (s // 1000, s % 1000, len(sub))); sub += p
open(sys.argv[1] + ".sub", "wb").write(sub); open(sys.argv[1] + ".idx", "w", encoding="utf-8").write("\n".join(idx) + "\n")
print("wrote", len(sub), "bytes")
