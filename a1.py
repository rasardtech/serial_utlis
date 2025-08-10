#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tek Dosya: ESC/POS Türevi Yazıcı İçin Adaptif Etiket Baskı Aracı
----------------------------------------------------------------
Bu dosya önceki parçalara ayrılmış (printer_config, font_selector, label_renderer,
raster_sender, print_label, test_throttle) yapının tamamını TEK DOSYADA birleştirir.

ÖZET ÖZELLİKLER
- Handshake (senin verdiğin komut dizisi)
- GS v 0 (raster) şerit (strip) gönderimi + hız (throttle) / tampon koruma
- Geniş etiketleri iki faz (üst+alt) basma (buffer overflow riskini azaltır)
- ESC * (8-dot klasik) fallback
- Ubuntu / DejaVu font seçimi + ENV FONT_PATH override
- JSON stdin'den veri alarak etiket basma
- Test modları: satranç (chess) paterni, parametre matrisi hızlı deneme
- Ayarlanabilir bit_reverse / invert, stripe_height, gs_v0_mode, gecikmeler
- Opsiyonel status polling (DLE EOT 0x10 0x04 n)

KOMUT SATIRI MODLARI
1) Etiket bas (varsayılan): stdin'den JSON okur
   echo '{"product_name":"PİLİÇ","ean13":"8684617390017"}' | python label_printer_all_in_one.py

2) Test matrisi (hız/şerit denemeleri):
   python label_printer_all_in_one.py --test-matrix

3) Sadece satranç + etiket üst parça hızlı test:
   python label_printer_all_in_one.py --quick-test

4) Yardım:
   python label_printer_all_in_one.py -h

JSON ÖRNEĞİ
{
  "product_name": "PİLİÇ SOSİS BÜFE 1000 G",
  "net_weight": "1,000 KG",
  "expiry_date": "2025-11-29",
  "ean13": "8684617390017",
  "ingredients": [
    "EMÜLSİFİYE ET ÜRÜNÜ",
    "İÇİNDEKİLER: PİLİÇ ETİ, SU, BAHARAT, TUZ",
    "ALERJEN: BİTKİSEL PROTEİN KAYNAĞI"
  ],
  "options": {
    "bit_reverse": false,
    "invert": false,
    "force_fallback": false,
    "split_two_phase": true,
    "stripe_height": 32,
    "gs_v0_mode": 0
  }
}

NOTLAR
- “Başarılı / bozuk” algısını otomatik yapmıyoruz; gözle kontrol.
- force_fallback=True gönderirsen direkt ESC * kullanır.
- Eğer büyük etiket bozuluyorsa önce stripe_height küçült (32→24→16), sonra delay artır.

LİSANS / UYARI
Bu kod örnektir; gerçek cihaz zarar görmemesi için yüksek hız parametrelerini bilinçsiz değiştirmeyin.
"""

import sys
import os
import json
import time
import binascii
import argparse
from dataclasses import dataclass
from typing import List, Dict, Iterable, Optional, Tuple

try:
    import serial
except ImportError:
    print("pyserial gerekli: pip install pyserial", file=sys.stderr)
    sys.exit(1)

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Pillow gerekli: pip install pillow", file=sys.stderr)
    sys.exit(1)

# ==============================================================
# KONFİG / VARSAYILANLAR
# ==============================================================

PORT = "/dev/ttyACM1"
BAUD = 19200
DPI = 203

HANDSHAKE_SEQUENCE = [
    b"\x1b@\x1b@\x1b@\x1b@\x1b@\xaaU",
    b"\x1b=\x01",
    b"\x12\x45\x01",
    b"\x12\x70\x03\x00",
]

DEFAULT_WIDTH_MM = 48.0       # 50 yerine 48: 384 dot (48 byte)
DEFAULT_HEIGHT_MM = 50.0

SPLIT_TWO_PHASE_DEFAULT = True
SPLIT_PART_LINES = 200
FEED_BETWEEN_PARTS_LF = 4

STRIPE_HEIGHT_CANDIDATES = [32, 24, 16]
DEFAULT_STRIPE_HEIGHT = 32
STRIPE_DELAY = 0.05
STRIPE_GROUP_SIZE = 4
STRIPE_GROUP_DELAY = 0.40

ALLOW_ESC_STAR_FALLBACK = True
BIT_REVERSE_DEFAULT = False
INVERT_DEFAULT = False
GS_V0_MODE_CANDIDATES = [0, 1, 2]

STATUS_POLL_ENABLED = False
STATUS_FUNCTION = 1
STATUS_TIMEOUT = 0.30
STATUS_READ_CHUNK = 64

FONT_SIZE_BIG = 32
FONT_SIZE_MID = 20
FONT_SIZE_SMALL = 16

VERBOSE = True

# Test matrisi için örnekler
TEST_MATRIX = [
    {"desc": "GSv0 m=0 sh=32", "mode": "gsv0", "gs_v0_mode": 0, "stripe_height": 32, "delay": 0.05},
    {"desc": "GSv0 m=0 sh=24", "mode": "gsv0", "gs_v0_mode": 0, "stripe_height": 24, "delay": 0.05},
    {"desc": "GSv0 m=0 sh=16", "mode": "gsv0", "gs_v0_mode": 0, "stripe_height": 16, "delay": 0.07},
    {"desc": "GSv0 m=1 sh=32", "mode": "gsv0", "gs_v0_mode": 1, "stripe_height": 32, "delay": 0.05},
    {"desc": "ESC* fallback",   "mode": "esc*", "stripe_height": None},
]

DEFAULT_FONT_CANDIDATES = [
    # Ubuntu
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-M.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
    "/usr/share/fonts/truetype/ubuntu/UbuntuMono-R.ttf",
    # DejaVu
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    # mac/Win
    "/Library/Fonts/Ubuntu-R.ttf",
    "/Library/Fonts/DejaVuSans.ttf",
    "C:/Windows/Fonts/Ubuntu-R.ttf",
    "C:/Windows/Fonts/DejaVuSans.ttf",
]


@dataclass
class PrintOptions:
    width_mm: float = DEFAULT_WIDTH_MM
    height_mm: float = DEFAULT_HEIGHT_MM
    rotate_180: bool = True
    bit_reverse: bool = BIT_REVERSE_DEFAULT
    invert: bool = INVERT_DEFAULT
    split_two_phase: bool = SPLIT_TWO_PHASE_DEFAULT
    upper_part_lines: int = SPLIT_PART_LINES
    feed_between_parts_lf: int = FEED_BETWEEN_PARTS_LF
    stripe_height: int = DEFAULT_STRIPE_HEIGHT
    gs_v0_mode: int = 0
    stripe_delay: float = STRIPE_DELAY
    stripe_group_size: int = STRIPE_GROUP_SIZE
    stripe_group_delay: float = STRIPE_GROUP_DELAY
    allow_esc_star_fallback: bool = ALLOW_ESC_STAR_FALLBACK
    force_fallback: bool = False
    status_poll: bool = STATUS_POLL_ENABLED
    font_big: int = FONT_SIZE_BIG
    font_mid: int = FONT_SIZE_MID
    font_small: int = FONT_SIZE_SMALL
    full_single_pass: bool = False


# ==============================================================
# FONT SEÇİMİ
# ==============================================================

def pick_font_path(extra: Optional[Iterable[str]] = None) -> Optional[str]:
    env_font = os.environ.get("FONT_PATH")
    if env_font and os.path.exists(env_font):
        return env_font
    if extra:
        for p in extra:
            if p and os.path.exists(p):
                return p
    for p in DEFAULT_FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def load_font(size: int, extra: Optional[Iterable[str]] = None):
    try:
        p = pick_font_path(extra)
        if p:
            f = ImageFont.truetype(p, size)
            if VERBOSE:
                print(f"[FONT] Using {p} size={size}")
            return f
    except Exception as e:
        print(f"[FONT] Hata: {e}")
    if VERBOSE:
        print("[FONT] Fallback PIL default")
    return ImageFont.load_default()


# ==============================================================
# ETİKET / CHESS GÖRÜNTÜ ÜRETİMİ
# ==============================================================

# Barkod (EAN13) tabloları
_L = {'0':'0001101','1':'0011001','2':'0010011','3':'0111101','4':'0100011','5':'0110001','6':'0101111','7':'0111011','8':'0110111','9':'0001011'}
_G = {'0':'0100111','1':'0110011','2':'0011011','3':'0100001','4':'0011101','5':'0111001','6':'0000101','7':'0010001','8':'0001001','9':'0010111'}
_R = {'0':'1110010','1':'1100110','2':'1101100','3':'1000010','4':'1011100','5':'1001110','6':'1010000','7':'1000100','8':'1001000','9':'1110100'}
_PAR = {'0':"LLLLLL",'1':"LLGLGG",'2':"LLGGLG",'3':"LLGGGL",'4':"LGLLGG",
        '5':"LGGLLG",'6':"LGGGLL",'7':"LGLGLG",'8':"LGLGGL",'9':"LGGLGL"}


def mm_to_dots(mm: float) -> int:
    return int(round(mm * DPI / 25.4))


def _ean_check(code12: str) -> str:
    s = 0
    for i, ch in enumerate(code12):
        d = int(ch)
        s += d if (i % 2 == 0) else d * 3
    return str((10 - s % 10) % 10)


def norm_ean13(raw: str) -> str:
    ds = ''.join(c for c in raw if c.isdigit())
    if len(ds) == 12:
        return ds + _ean_check(ds)
    if len(ds) == 13:
        return ds
    raise ValueError("EAN13 uzunluk 12/13 değil")


def draw_ean13(draw, x, y, code: str, module_w=2, bar_h=54, font=None) -> int:
    full = norm_ean13(code)
    first = full[0]; left = full[1:7]; right = full[7:]
    pattern = "101"
    parity = _PAR[first]
    for d, p in zip(left, parity):
        pattern += (_L[d] if p == 'L' else _G[d])
    pattern += "01010"
    for d in right:
        pattern += _R[d]
    pattern += "101"
    for i, bit in enumerate(pattern):
        if bit == '1':
            draw.rectangle([x + i * module_w, y, x + (i + 1) * module_w - 1, y + bar_h], fill=0)
    ty = y + bar_h + 2
    if font:
        draw.text((x, ty), full, font=font, fill=0)
        return bar_h + font.size + 4
    else:
        draw.text((x, ty), full, fill=0)
        return bar_h + 14


def build_label_image(data: Dict,
                      width_mm: float,
                      height_mm: float,
                      font_big: int,
                      font_mid: int,
                      font_small: int,
                      rotate_180: bool = True) -> Image.Image:
    w = mm_to_dots(width_mm)
    h = mm_to_dots(height_mm)
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)

    f_big = load_font(font_big)
    f_mid = load_font(font_mid)
    f_sm = load_font(font_small)

    xpad = 10
    y = 6
    product = data.get("product_name", "ÜRÜN ADI")
    d.text((xpad, y), product, font=f_big, fill=0)
    y += f_big.size + 4

    net_weight = data.get("net_weight", "")
    expiry = data.get("expiry_date", "")
    if net_weight:
        d.text((xpad, y), f"Ağırlık: {net_weight}", font=f_mid, fill=0)
    if expiry:
        exp_txt = f"S.T.T: {expiry}"
        exp_w = d.textlength(exp_txt, font=f_mid)
        d.text((w - exp_w - xpad, y), exp_txt, font=f_mid, fill=0)
    y += f_mid.size + 6

    ean = data.get("ean13")
    if ean:
        used = draw_ean13(d, xpad, y, ean, module_w=2, bar_h=52, font=f_sm)
        y += used + 4

    ingredients: List[str] = data.get("ingredients", [])
    for line in ingredients:
        if y + f_sm.size + 2 > h - 6:
            break
        d.text((xpad, y), line, font=f_sm, fill=0)
        y += f_sm.size + 2

    if rotate_180:
        img = img.rotate(180)
    if VERBOSE:
        print(f"[LABEL] size={img.size}")
    return img.convert("1")


def build_chess(width_dots: int, height_dots: int, block: int = 8, rotate_180: bool = True) -> Image.Image:
    img = Image.new("1", (width_dots, height_dots), 1)
    px = img.load()
    for y in range(height_dots):
        for x in range(width_dots):
            blk = ((x // block + y // block) % 2) == 0
            px[x, y] = 0 if blk else 1
    if rotate_180:
        img = img.rotate(180)
    return img


# ==============================================================
# RASTER / GÖNDERİM
# ==============================================================

def hex_dump(label: str, data: bytes, limit: int = 32):
    if VERBOSE:
        print(f"[HEX {label}] {len(data)} bytes first={binascii.hexlify(data[:limit]).decode()}")


def reverse_bits(b: int) -> int:
    b = (b & 0xF0) >> 4 | (b & 0x0F) << 4
    b = (b & 0xCC) >> 2 | (b & 0x33) << 2
    b = (b & 0xAA) >> 1 | (b & 0x55) << 1
    return b


def transform_bytes(data: bytes, bit_reverse: bool, invert: bool) -> bytes:
    if not bit_reverse and not invert:
        return data
    out = bytearray(len(data))
    for i, B in enumerate(data):
        v = B
        if invert:
            v ^= 0xFF
        if bit_reverse:
            v = reverse_bits(v)
        out[i] = v
    return bytes(out)


def pack_1bit(img: Image.Image) -> Tuple[bytes, int, int]:
    if img.mode != "1":
        img = img.convert("1")
    w, h = img.size
    row_bytes = (w + 7) // 8
    px = img.load()
    out = bytearray(row_bytes * h)
    for y in range(h):
        byte = 0
        bits = 0
        idx = y * row_bytes
        for x in range(w):
            bit = 1 if px[x, y] == 0 else 0
            byte = (byte << 1) | bit
            bits += 1
            if bits == 8:
                out[idx] = byte
                idx += 1
                byte = 0
                bits = 0
        if bits:
            byte <<= (8 - bits)
            out[idx] = byte
    return bytes(out), row_bytes, h


def do_handshake(ser):
    if VERBOSE:
        print("[INFO] Handshake start")
    for i, c in enumerate(HANDSHAKE_SEQUENCE):
        ser.write(c)
        ser.flush()
        time.sleep(0.05 if i < len(HANDSHAKE_SEQUENCE) - 1 else 0.10)
    if VERBOSE:
        print("[INFO] Handshake done")


def poll_status(ser):
    if not STATUS_POLL_ENABLED:
        return
    cmd = bytes([0x10, 0x04, STATUS_FUNCTION])
    ser.write(cmd)
    ser.flush()
    ser.timeout = STATUS_TIMEOUT
    resp = ser.read(STATUS_READ_CHUNK)
    if VERBOSE:
        print(f"[STATUS] {binascii.hexlify(resp).decode()}")


def send_gsv0_strips(
    ser,
    data: bytes,
    row_bytes: int,
    height: int,
    stripe_h: int,
    gs_v0_mode: int,
    bit_reverse: bool,
    invert: bool,
    stripe_delay: float,
    stripe_group_size: int,
    stripe_group_delay: float
):
    tdata = transform_bytes(data, bit_reverse, invert)
    total = 0
    stripe_index = 0
    for y0 in range(0, height, stripe_h):
        chunk_h = min(stripe_h, height - y0)
        off = y0 * row_bytes
        chunk = tdata[off:off + row_bytes * chunk_h]
        xL = row_bytes & 0xFF
        xH = (row_bytes >> 8) & 0xFF
        yL = chunk_h & 0xFF
        yH = (chunk_h >> 8) & 0xFF
        m = gs_v0_mode & 0xFF
        header = bytes([0x1D, 0x76, 0x30, m, xL, xH, yL, yH])
        ser.write(header + chunk)
        ser.flush()
        total += len(chunk)
        hex_dump(f"STRIP y={y0} h={chunk_h}", header + chunk[:16])
        poll_status(ser)
        stripe_index += 1
        time.sleep(stripe_delay)
        if stripe_group_size > 0 and stripe_index % stripe_group_size == 0:
            time.sleep(stripe_group_delay)
    ser.write(b"\n")
    ser.flush()
    if VERBOSE:
        print(f"[INFO] GSv0 Strips Done payload={total}")


def send_esc_star(
    ser,
    data: bytes,
    row_bytes: int,
    height: int,
    bit_reverse: bool,
    invert: bool,
    inter_block_delay: float = 0.01
):
    tdata = transform_bytes(data, bit_reverse, invert)
    blocks = (height + 7) // 8
    for b in range(blocks):
        block_bytes = bytearray()
        for line in range(8):
            y = b * 8 + line
            if y >= height:
                block_bytes.extend(b'\x00' * row_bytes)
            else:
                off = y * row_bytes
                block_bytes.extend(tdata[off:off + row_bytes])
        nL = row_bytes & 0xFF
        nH = (row_bytes >> 8) & 0xFF
        header = bytes([0x1B, ord('*'), 0x00, nL, nH])
        ser.write(header + block_bytes)
        ser.flush()
        hex_dump(f"ESC* blk={b}", header + block_bytes[:16])
        time.sleep(inter_block_delay)
    ser.write(b"\n")
    ser.flush()
    if VERBOSE:
        print(f"[INFO] ESC* Done blocks={blocks}")


def adaptive_print(
    ser,
    image: Image.Image,
    opts: PrintOptions,
    use_split: bool = True,
    fallback_allowed: bool = True
) -> bool:
    """
    Basit adaptif strateji:
    - GS v 0 (seçilmiş stripe + gs_v0_mode) (tek veya iki faz)
    - (force_fallback=True) ise doğrudan ESC *
    Not: Otomatik "başarısızlık" algısı yok; kullanıcı gözle değerlendirecek.
    """
    data, rb, h = pack_1bit(image)

    def two_phase(gs_mode: int, stripe_h: int):
        upper = min(opts.upper_part_lines, h)
        lower = max(0, h - upper)
        if upper > 0:
            send_gsv0_strips(
                ser, data[:rb * upper], rb, upper,
                stripe_h, gs_mode,
                opts.bit_reverse, opts.invert,
                opts.stripe_delay, opts.stripe_group_size, opts.stripe_group_delay
            )
        if opts.feed_between_parts_lf > 0:
            ser.write(b"\n" * opts.feed_between_parts_lf)
            ser.flush()
            time.sleep(0.3)
        if lower > 0:
            send_gsv0_strips(
                ser, data[rb * upper:], rb, lower,
                stripe_h, gs_mode,
                opts.bit_reverse, opts.invert,
                opts.stripe_delay, opts.stripe_group_size, opts.stripe_group_delay
            )

    def single_phase(gs_mode: int, stripe_h: int):
        send_gsv0_strips(
            ser, data, rb, h,
            stripe_h, gs_mode,
            opts.bit_reverse, opts.invert,
            opts.stripe_delay, opts.stripe_group_size, opts.stripe_group_delay
        )

    if opts.force_fallback:
        if VERBOSE:
            print("[FALLBACK] ESC * force")
        send_esc_star(ser, data, rb, h, opts.bit_reverse, opts.invert)
        return True

    if use_split and opts.split_two_phase:
        two_phase(opts.gs_v0_mode, opts.stripe_height)
    else:
        single_phase(opts.gs_v0_mode, opts.stripe_height)

    if fallback_allowed and opts.allow_esc_star_fallback:
        if VERBOSE:
            print("[INFO] Eğer çıktı bozuksa tekrar --force-fallback ile ESC* dene.")
    return True


# ==============================================================
# GİRİŞ / JSON / ARGUMENT PARSING
# ==============================================================

def read_stdin_json() -> Dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def build_options_from_json(js: Dict) -> PrintOptions:
    opt = js.get("options", {})
    return PrintOptions(
        bit_reverse=opt.get("bit_reverse", BIT_REVERSE_DEFAULT),
        invert=opt.get("invert", INVERT_DEFAULT),
        force_fallback=opt.get("force_fallback", False),
        rotate_180=opt.get("rotate_180", True),
        width_mm=opt.get("width_mm", DEFAULT_WIDTH_MM),
        height_mm=opt.get("height_mm", DEFAULT_HEIGHT_MM),
        split_two_phase=opt.get("split_two_phase", SPLIT_TWO_PHASE_DEFAULT),
        stripe_height=opt.get("stripe_height", DEFAULT_STRIPE_HEIGHT),
        gs_v0_mode=opt.get("gs_v0_mode", 0),
        stripe_delay=opt.get("stripe_delay", STRIPE_DELAY),
        stripe_group_size=opt.get("stripe_group_size", STRIPE_GROUP_SIZE),
        stripe_group_delay=opt.get("stripe_group_delay", STRIPE_GROUP_DELAY),
        allow_esc_star_fallback=opt.get("allow_esc_star_fallback", ALLOW_ESC_STAR_FALLBACK),
        font_big=opt.get("font_big", FONT_SIZE_BIG),
        font_mid=opt.get("font_mid", FONT_SIZE_MID),
        font_small=opt.get("font_small", FONT_SIZE_SMALL),
        upper_part_lines=opt.get("upper_part_lines", SPLIT_PART_LINES),
        feed_between_parts_lf=opt.get("feed_between_parts_lf", FEED_BETWEEN_PARTS_LF),
        status_poll=opt.get("status_poll", STATUS_POLL_ENABLED),
        full_single_pass=opt.get("full_single_pass", False),
    )


# ==============================================================
# TEST ARAÇLARI
# ==============================================================

def run_test_matrix():
    print(f"[INFO] Test Matrix başlıyor PORT={PORT}@{BAUD}")
    try:
        ser = serial.Serial(PORT, BAUD, timeout=1)
    except Exception as e:
        print("[ERR] Seri port açılamadı:", e, file=sys.stderr)
        return
    with ser:
        do_handshake(ser)
        for cfg in TEST_MATRIX:
            print("\n==============================")
            print(f"[TEST] {cfg['desc']}")
            stripe_h = cfg.get("stripe_height") or 32
            gs_mode = cfg.get("gs_v0_mode", 0)
            delay = cfg.get("delay", 0.05)
            # Chess
            w = mm_to_dots(DEFAULT_WIDTH_MM)
            chess_img = build_chess(w, 128, 8, rotate_180=True)
            chess_data, chess_rb, chess_h = pack_1bit(chess_img)
            if cfg["mode"] == "gsv0":
                send_gsv0_strips(
                    ser, chess_data, chess_rb, chess_h,
                    stripe_h, gs_mode,
                    BIT_REVERSE_DEFAULT, INVERT_DEFAULT,
                    delay, STRIPE_GROUP_SIZE, STRIPE_GROUP_DELAY
                )
            else:
                send_esc_star(
                    ser, chess_data, chess_rb, chess_h,
                    BIT_REVERSE_DEFAULT, INVERT_DEFAULT
                )
            time.sleep(0.6)
            # Üst parça (label stub)
            label_stub = {
                "product_name": "TEST ÜRÜN",
                "net_weight": "1,000 KG",
                "expiry_date": "2025-11-29",
                "ean13": "8684617390017",
                "ingredients": ["DENEME SATIRI 1", "DENEME SATIRI 2"]
            }
            label_img = build_label_image(
                label_stub, DEFAULT_WIDTH_MM, DEFAULT_HEIGHT_MM,
                FONT_SIZE_BIG, FONT_SIZE_MID, FONT_SIZE_SMALL,
                rotate_180=True
            )
            l_data, rb, h = pack_1bit(label_img)
            upper = min(200, h)
            if cfg["mode"] == "gsv0":
                send_gsv0_strips(
                    ser, l_data[:rb * upper], rb, upper,
                    stripe_h, gs_mode,
                    BIT_REVERSE_DEFAULT, INVERT_DEFAULT,
                    delay, STRIPE_GROUP_SIZE, STRIPE_GROUP_DELAY
                )
            else:
                send_esc_star(
                    ser, l_data[:rb * upper], rb, upper,
                    BIT_REVERSE_DEFAULT, INVERT_DEFAULT
                )
            print("[INFO] Sonraki deneme için 2s bekleme...")
            time.sleep(2)
    print("\n[FINISH] Test Matrix tamam. Gözlemini not et.")


def run_quick_test():
    print(f"[INFO] Quick Test PORT={PORT}@{BAUD}")
    try:
        ser = serial.Serial(PORT, BAUD, timeout=1)
    except Exception as e:
        print("[ERR] Port açılamadı:", e, file=sys.stderr)
        return
    with ser:
        do_handshake(ser)
        # Chess
        w = mm_to_dots(DEFAULT_WIDTH_MM)
        chess_img = build_chess(w, 128, 8, rotate_180=True)
        chess_data, chess_rb, chess_h = pack_1bit(chess_img)
        send_gsv0_strips(
            ser, chess_data, chess_rb, chess_h,
            DEFAULT_STRIPE_HEIGHT, 0,
            BIT_REVERSE_DEFAULT, INVERT_DEFAULT,
            STRIPE_DELAY, STRIPE_GROUP_SIZE, STRIPE_GROUP_DELAY
        )
        time.sleep(0.8)
        # Üst parça label
        label_stub = {
            "product_name": "QUICK TEST",
            "net_weight": "1,000 KG",
            "expiry_date": "2025-11-29",
            "ean13": "8684617390017",
            "ingredients": ["SATIR1", "SATIR2", "SATIR3"]
        }
        label_img = build_label_image(
            label_stub, DEFAULT_WIDTH_MM, DEFAULT_HEIGHT_MM,
            FONT_SIZE_BIG, FONT_SIZE_MID, FONT_SIZE_SMALL,
            rotate_180=True
        )
        data, rb, h = pack_1bit(label_img)
        upper = min(200, h)
        send_gsv0_strips(
            ser, data[:rb * upper], rb, upper,
            DEFAULT_STRIPE_HEIGHT, 0,
            BIT_REVERSE_DEFAULT, INVERT_DEFAULT,
            STRIPE_DELAY, STRIPE_GROUP_SIZE, STRIPE_GROUP_DELAY
        )
    print("[FINISH] Quick test bitti.")


# ==============================================================
# ETİKET BASKI (JSON)
# ==============================================================

def run_print_from_json():
    js = read_stdin_json()
    if not js:
        print("[ERR] STDIN boş. JSON verisi bekleniyor.", file=sys.stderr)
        sys.exit(1)
    opts = build_options_from_json(js)
    if VERBOSE:
        print(f"[INFO] Port açılıyor: {PORT}@{BAUD}")
    try:
        ser = serial.Serial(PORT, BAUD, timeout=1)
    except Exception as e:
        print("[ERR] Seri port açılamadı:", e, file=sys.stderr)
        sys.exit(2)
    with ser:
        do_handshake(ser)
        img = build_label_image(
            js,
            opts.width_mm,
            opts.height_mm,
            opts.font_big,
            opts.font_mid,
            opts.font_small,
            rotate_180=opts.rotate_180
        )
        adaptive_print(ser, img, opts, use_split=opts.split_two_phase, fallback_allowed=True)
    if VERBOSE:
        print("[DONE] Baskı tamamlandı.")


# ==============================================================
# ARG PARSER
# ==============================================================

def build_arg_parser():
    p = argparse.ArgumentParser(
        description="Tek dosya ESC/POS adaptif etiket yazdırma aracı.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    p.add_argument("--test-matrix", action="store_true", help="Parametre matrisi testini çalıştır")
    p.add_argument("--quick-test", action="store_true", help="Satranç + kısmi etiket hızlı test")
    return p


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.test_matrix:
        run_test_matrix()
    elif args.quick_test:
        run_quick_test()
    else:
        run_print_from_json()


if __name__ == "__main__":
    main()