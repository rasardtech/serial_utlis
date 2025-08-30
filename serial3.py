from __future__ import annotations

# Terazi Etiket Yazıcı – Tek Dosya (font büyütme + canlı konum ayarı)
# - Sans Serif font (normal + bold) zorunlu (sistem TTF taraması).
# - Ürün adı barkodun hemen üstünde sola yaslı.
# - “ALERJEN UYARISI …” satırı kalın (bold).
# - Fiziksel konum kalibrasyonu: aşağı/sola canlı ayar (GUI butonları ve spinbox).
# - COM6 terazi fallback, ham veri görünümü, POLL/LISTEN okuma modları.
# - 15 kg üzeri veya "400000.00 g" gibi hatalı ölçüleri yok sayar.
# - Odoo START/DONE ve print_series akışları.
#
# Gereksinimler:
#   pip install pillow pyserial requests
#
# Hızlı kalibrasyon ipuçları:
# - “Aşağı” istiyorsanız PHYS_SHIFT_DOWN_MM değerini büyütün (veya GUI’de “Aşağı +0.5 mm”).
# - “Sola” istiyorsanız H_SHIFT_MM değerini büyütün (veya GUI’de “Sola +0.5 mm”).
# - Varsayılanlar: PHYS_SHIFT_DOWN_MM=10 mm, H_SHIFT_MM=3.0 mm (GUI’den anında değişebilir).

import os
import re
import json
import time
import math
import threading
import queue
from collections import deque
from typing import Tuple, Dict, Any, List, Optional

import requests
import serial
from serial.tools import list_ports

from PIL import Image, ImageDraw, ImageFont, ImageTk

import tkinter as tk
from tkinter import ttk, messagebox

# =========================
# Odoo Uçları ve Kararlılık
# =========================
GET_JOB_URL = "https://altinayet-stage-22335048.dev.odoo.com/terazi/get_scale_job/1"   # scale_id'yi gerektiği gibi güncelleyin
ODOO_URL_TEMPLATE = "https://altinayet-stage-22335048.dev.odoo.com/terazi/get/{mrp_id}/{weight}"

STABLE_COUNT = 5
SENSITIVITY_GRAM = 20
MAX_REALISTIC_GRAMS = 15000  # 15 kg üstünü yok say

# =========================
# Yazıcı (ESC 'V' raster) Ayarları
# =========================
IS_WINDOWS = os.name == "nt"

PREVIEW_PNG_PATH = "label_preview.png"
PREVIEW_BMP1_PATH = "label_preview_1b.bmp"
PREVIEW_BIN_PATH  = "label_raster_padded.bin"

PRN_PORT_FALLBACK = "COM3" if IS_WINDOWS else "/dev/ttyACM0"
PRN_BAUD = 19200
PRN_PARITY = serial.PARITY_NONE
PRN_TIMEOUT = 0.5

# Cihaz genişliği (bayt/satır)
DEVICE_WIDTH_BYTES = 108
DEVICE_WIDTH_DOTS  = DEVICE_WIDTH_BYTES * 8  # 864 dot

DATA_CHUNK_SIZE = 4096
FEED_AFTER_LINES = 0  # Başta boş satır istemediğimiz için 0

# =========================
# Tuval ve raster işleme
# =========================
REQ_W = 748
REQ_H = 748
BOTTOM_FORBID = 120
ROTATE_180 = True
THRESHOLD = 192
INVERT_BW = False

def round_to_8(n: int) -> int:
    return int(math.ceil(n / 8.0) * 8)

WIDTH_DOTS = round_to_8(REQ_W)   # 752
HEIGHT_DOTS = REQ_H
LABEL_WIDTH_BYTES = WIDTH_DOTS // 8  # 94

# 203 dpi ~ 8 dot/mm
DPMM = 8

def _env_float(name: str, default_val: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else default_val
    except Exception:
        return default_val

# Varsayılan fiziksel kalibrasyon (GUI’den canlı değiştirilecek)
PHYS_SHIFT_DOWN_MM = _env_float("PHYS_SHIFT_DOWN_MM", _env_float("V_SHIFT_MM", 10.0))  # 10 mm aşağı
H_SHIFT_MM = _env_float("H_SHIFT_MM", 3.0)  # merkezden sola 3.0 mm

def mm_to_dots(mm: float) -> int:
    return int(round(mm * DPMM))

# İç yerleşim (px) – metin biraz büyüdüğü için blokları da hafif aşağı aldık
LEFT_MARGIN = 8               # içeriği sola yakın başlat
LEFT_BLOCK_Y = 148            # 140 -> 148 (biraz aşağı)
LEFT_BLOCK_GAP = 48           # 44 -> 48 (satır arası büyüdü)
LEFT_COL_WIDTH = 270
COL_GAP = 16
RIGHT_BARCODE_HEIGHT = 108

# =========================
# Sans Serif font çözümleme (normal + bold)
# =========================
def _scan_font_dirs() -> list[str]:
    dirs = []
    try:
        if IS_WINDOWS:
            dirs += [r"C:\Windows\Fonts"]
        # Linux/macOS
        dirs += [
            "/usr/share/fonts",
            "/usr/local/share/fonts",
            os.path.expanduser("~/.fonts"),
            os.path.expanduser("~/.local/share/fonts"),
            "/Library/Fonts",
            "/System/Library/Fonts",
        ]
    except Exception:
        pass
    return [d for d in dict.fromkeys(dirs) if os.path.isdir(d)]

def _find_font_by_names(names: list[str]) -> Optional[str]:
    font_dirs = _scan_font_dirs()
    for d in font_dirs:
        try:
            for root, _, files in os.walk(d):
                lower = {f.lower(): f for f in files}
                for name in names:
                    key = name.lower()
                    if key in lower:
                        return os.path.join(root, lower[key])
        except Exception:
            continue
    return None

def resolve_sans_serif_paths() -> tuple[Optional[str], Optional[str]]:
    normal_candidates = [
        "segoeui.ttf", "arial.ttf",                 # Windows
        "DejaVuSans.ttf", "LiberationSans-Regular.ttf", "FreeSans.ttf",  # Linux
        "Arial.ttf", "Helvetica.ttc", "HelveticaNeue.ttc",               # macOS
    ]
    bold_candidates = [
        "segoeuib.ttf", "arialbd.ttf",              # Windows
        "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "FreeSansBold.ttf",  # Linux
        "Arial Bold.ttf", "Helvetica-Bold.ttf", "HelveticaNeue-Bold.ttf",      # macOS
    ]
    return _find_font_by_names(normal_candidates), _find_font_by_names(bold_candidates)

FORCE_SANS_SERIF = os.getenv("FORCE_SANS_SERIF", "1") in ("1", "true", "True")  # varsayılan zorla
SANS_NORMAL_PATH, SANS_BOLD_PATH = resolve_sans_serif_paths()

def load_font_exact(path: Optional[str], size: int) -> ImageFont.ImageFont:
    if path and os.path.exists(path):
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            pass
    return ImageFont.load_default()

def get_fonts_for_sizes(size_title=30, size_sub=24, size_label=22, size_text=18, size_bar=18, payload_font_path: Optional[str] = None):
    # font point karşılığı (~203 dpi): pt ≈ px * 72 / 203
    # 30px≈10.65pt, 24px≈8.52pt, 22px≈7.80pt, 18px≈6.39pt (3pt sınırının oldukça üzerinde)
    normal_base = None
    bold_base = None
    if not FORCE_SANS_SERIF and payload_font_path and os.path.exists(payload_font_path):
        normal_base = payload_font_path
        base_dir = os.path.dirname(payload_font_path)
        base_name = os.path.splitext(os.path.basename(payload_font_path))[0]
        for suffix in ("-Bold.ttf", "Bold.ttf", "bd.ttf"):
            cand = os.path.join(base_dir, base_name + suffix)
            if os.path.exists(cand):
                bold_base = cand
                break
    if not normal_base:
        normal_base = SANS_NORMAL_PATH
    if not bold_base:
        bold_base = SANS_BOLD_PATH

    f_title = load_font_exact(normal_base, size_title)
    f_sub   = load_font_exact(normal_base, size_sub)
    f_label = load_font_exact(normal_base, size_label)
    f_text  = load_font_exact(normal_base, size_text)
    f_text_b= load_font_exact(bold_base,   size_text)
    f_bar   = load_font_exact(normal_base, size_bar)
    return {
        "title": f_title, "sub": f_sub, "label": f_label, "text": f_text, "text_b": f_text_b, "bar": f_bar,
        "_paths": {"normal": normal_base, "bold": bold_base}
    }

# =========================
# Terazi (AD2K) Ayarları
# =========================
SCL_BAUD = 19200
SCL_PARITY = serial.PARITY_ODD
SCL_TIMEOUT = 0.5

# Windows'ta COM6'yı varsayılan tercih et (TERAZI_PORT ile override edilebilir)
SCL_PORT_FALLBACK = "COM6" if IS_WINDOWS else "/dev/ttyUSB0"

# =========================
# Barkod (EAN-13)
# =========================
EAN_L = {'0': "0001101",'1': "0011001",'2': "0010011",'3': "0111101",'4': "0100011",'5': "0110001",'6': "0101111",'7': "0111011",'8': "0110111",'9': "0001011"}
EAN_G = {'0': "0100111",'1': "0110011",'2': "0011011",'3': "0100001",'4': "0011101",'5': "0111001",'6': "0000101",'7': "0010001",'8': "0001001",'9': "0010111"}
EAN_R = {'0': "1110010",'1': "1100110",'2': "1101100",'3': "1000010",'4': "1011100",'5': "1001110",'6': "1010000",'7': "1000100",'8': "1001000",'9': "1110100"}
EAN_PARITY = {'0': "LLLLLL",'1': "LLGLGG",'2': "LLGGLG",'3': "LLGGGL",'4': "LGLLGG",'5': "LGGLLG",'6': "LGGGLL",'7': "LGLGLG",'8': "LGLGGL",'9': "LGGLGL"}

def ean13_check_digit(data12: str) -> str:
    s = 0
    for i, ch in enumerate(data12):
        n = ord(ch) - 48
        s += n if (i % 2) == 0 else 3 * n
    return str((10 - (s % 10)) % 10)

def draw_ean13(canvas: Image.Image, x: int, y: int, width: int, height: int, data: str, font: ImageFont.ImageFont):
    draw = ImageDraw.Draw(canvas)
    digits = "".join(ch for ch in (data or "") if ch.isdigit())
    if len(digits) not in (12, 13):
        draw.rectangle([x, y, x+width, y+height], outline=(0,0,0), width=2)
        draw.text((x+4, y+height- font.size - 2), digits or "EAN13?", font=font, fill=(0,0,0))
        return
    if len(digits) == 12:
        digits += ean13_check_digit(digits)

    modules = 95
    mw = max(1, width // modules)
    bw = modules * mw
    x0 = x + (width - bw) // 2

    first = digits[0]; left = digits[1:7]; right = digits[7:]
    parity = EAN_PARITY.get(first, "LLLLLL")

    pattern = "101"
    for i, ch in enumerate(left):
        pattern += (EAN_L if parity[i]=='L' else EAN_G)[ch]
    pattern += "01010"
    for ch in right:
        pattern += EAN_R[ch]
    pattern += "101"

    text_h = max(12, int(height * 0.18))
    bar_h = max(1, height - text_h - 4)

    for i, bit in enumerate(pattern):
        if bit == '1':
            x1 = x0 + i * mw
            draw.rectangle([x1, y, x1 + mw - 1, y + bar_h], fill=(0,0,0))

    num_text = f"{first} {left} {right}"
    tw = int(draw.textlength(num_text, font=font))
    draw.text((x0 + (bw - tw)//2, y + bar_h + 2), num_text, font=font, fill=(0,0,0))

# =========================
# Metin sarmalama
# =========================
def text_wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    if not text:
        return ""
    lines: List[str] = []
    for para in text.splitlines():
        if not para:
            lines.append("")
            continue
        words = para.split(" ")
        buf = ""
        for w in words:
            cand = w if not buf else f"{buf} {w}"
            if draw.textlength(cand, font=font) <= max_width:
                buf = cand
            else:
                if buf:
                    lines.append(buf)
                buf = w
        if buf:
            lines.append(buf)
    return "\n".join(lines)

# =========================
# Görsel bileşimi
# =========================
def compose_label(data: Dict[str, Any], width_dots: int, height_dots: int, forbid_bottom_px: int) -> Image.Image:
    payload_font_path = data.get("font_path")
    fonts = get_fonts_for_sizes(
        size_title=30, size_sub=24, size_label=22, size_text=18, size_bar=18,
        payload_font_path=payload_font_path
    )
    f_title = fonts["title"]; f_sub = fonts["sub"]; f_label = fonts["label"]; f_text = fonts["text"]; f_text_b = fonts["text_b"]; f_bar = fonts["bar"]

    canvas = Image.new("RGB", (width_dots, height_dots), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    # Sol blok
    y0 = LEFT_BLOCK_Y
    left_x = LEFT_MARGIN
    label_w = 120
    val_x = left_x + label_w + 8

    draw.text((left_x, y0), "Adet:",  font=f_label, fill=(0,0,0))
    draw.text((val_x,  y0), str(data.get("count", "")), font=f_label, fill=(0,0,0))

    y1 = y0 + LEFT_BLOCK_GAP
    draw.text((left_x, y1), "Ağırlık:", font=f_label, fill=(0,0,0))
    draw.text((val_x,  y1), str(data.get("weight_str", "")), font=f_sub,   fill=(0,0,0))

    y2 = y1 + LEFT_BLOCK_GAP
    draw.text((left_x, y2), "S.T.T.:", font=f_label, fill=(0,0,0))
    draw.text((val_x,  y2), str(data.get("expiry", "")), font=f_label, fill=(0,0,0))

    # Sağ sütun: Ürün adı + barkod
    right_x = LEFT_MARGIN + LEFT_COL_WIDTH + COL_GAP
    right_w = max(200, width_dots - right_x - LEFT_MARGIN)
    bar_x = right_x
    bar_top = y0
    bar_h = RIGHT_BARCODE_HEIGHT

    # Ürün adı – barkodun hemen üstünde, sola yaslı; tek satıra sığması için 30→20 px arası küçült
    product = str(data.get("product_name", "") or "").strip()
    if product:
        size = f_title.size
        normal_path = fonts["_paths"]["normal"]
        while size >= 20 and draw.textlength(product, font=load_font_exact(normal_path, size)) > right_w:
            size -= 1
        f_prod = load_font_exact(normal_path, size)
        prod_y = max(0, bar_top - f_prod.size - 4)  # barkodun hemen üstü, ~4 px boşluk
        draw.text((right_x, prod_y), product, font=f_prod, fill=(0,0,0))

    # Barkod
    draw_ean13(canvas, bar_x, bar_top, right_w, bar_h, str(data.get("barcode", "")), f_bar)

    # İç metin bloğu (altta) – Alerjen satırı bold
    last_left_y = y2 + f_label.size
    last_barcode_y = bar_top + bar_h + max(12, int(RIGHT_BARCODE_HEIGHT * 0.18)) + 4
    text_top = max(last_left_y, last_barcode_y) + 16

    safe_h = height_dots - forbid_bottom_px
    block_h = max(0, safe_h - text_top - 8)
    block_w = width_dots - 2*LEFT_MARGIN

    paragraph_texts: List[str] = []
    for key in ("ingredients", "notes"):
        t = str(data.get(key, "") or "").strip()
        if t:
            paragraph_texts.append(t)
    full_text = "\n\n".join(paragraph_texts)

    if block_h > 0 and full_text:
        y = text_top
        for para in full_text.split("\n"):
            is_allergen = para.strip().upper().startswith("ALERJEN")
            f_wrap = f_text_b if is_allergen else f_text
            wrapped = text_wrap(draw, para, f_wrap, block_w)
            for ln in wrapped.splitlines():
                line_h = f_wrap.size + 6
                if y + line_h > text_top + block_h:
                    if draw.textlength("...", font=f_wrap) <= block_w:
                        draw.text((LEFT_MARGIN, y), "...", font=f_wrap, fill=(0,0,0))
                    y = text_top + block_h
                    break
                draw.text((LEFT_MARGIN, y), ln, font=f_wrap, fill=(0,0,0))
                y += line_h
            if y >= text_top + block_h:
                break

    if ROTATE_180:
        canvas = canvas.rotate(180, expand=False)
    return canvas

# =========================
# Görsel kaydırma ve raster dönüşüm
# =========================
def shift_image_vertical(img: Image.Image, dy: int, fill=(255, 255, 255)) -> Image.Image:
    w, h = img.size
    out = Image.new(img.mode, (w, h), fill)
    out.paste(img, (0, dy))
    return out

def to_1bit_bytes(img: Image.Image, width_dots: int, threshold: int = THRESHOLD, invert: bool = INVERT_BW) -> Tuple[bytes, int, int]:
    w, h = img.size
    assert w == width_dots, f"Image width {w} != {width_dots}"
    bw = img.convert("L").point(lambda p: 0 if p < threshold else 255, "L")
    width_bytes = width_dots // 8
    raw = bytearray(width_bytes * h)
    p = bw.load()
    for y in range(h):
        off = y * width_bytes
        val = 0
        bitc = 0
        xb = 0
        for x in range(width_dots):
            bit = 1 if ((p[x, y] == 0) ^ invert) else 0
            val = ((val << 1) | bit) & 0xFF
            bitc += 1
            if bitc == 8:
                raw[off + xb] = val
                val = 0
                bitc = 0
                xb += 1
    return bytes(raw), width_bytes, h

def pad_rows_to_device_width(raw: bytes, label_wb: int, device_wb: int, rows: int, align: str = "center", left_shift_dots: int = 0) -> bytes:
    assert device_wb >= label_wb
    out = bytearray(device_wb * rows)
    pad_total = device_wb - label_wb
    if align == "left":
        pad_left = 0
    elif align == "right":
        pad_left = pad_total
    else:
        pad_left = pad_total // 2
    # Merkezden sola doğru ilave kaydırma
    if left_shift_dots > 0:
        pad_left = max(0, min(pad_total, pad_left - left_shift_dots))
    for r in range(rows):
        src_off = r * label_wb
        dst_off = r * device_wb + pad_left
        out[dst_off:dst_off + label_wb] = raw[src_off:src_off + label_wb]
    return bytes(out)

# =========================
# Yazıcı protokolü
# =========================
def printer_handshake(ser: serial.Serial):
    seq = [
        b"\x1b@\x1b@\x1b@\x1b@\x1b@\xaa\x55",
        b"\x1b=\x01",
        b"\x12\x45\x01",
        b"\x12\x70\x03",
    ]
    for cmd in seq:
        ser.write(cmd); ser.flush()
        time.sleep(0.06)
        try:
            _ = ser.read(64)
        except Exception:
            pass

def clear_printer_buffer(ser: serial.Serial):
    try:
        ser.write(b"\x18")  # CAN
        ser.flush()
        time.sleep(0.03)
        _ = ser.read(64)
    except Exception:
        pass

def send_single_esc_v_height_only(ser: serial.Serial, raw_padded: bytes, rows: int, chunk_size: int = DATA_CHUNK_SIZE):
    nL, nH = rows & 0xFF, (rows >> 8) & 0xFF
    header = bytes([0x1B, 0x56, nL, nH])
    ser.write(header)
    ser.flush()
    time.sleep(0.01)

    total = len(raw_padded)
    sent = 0
    while sent < total:
        end = min(sent + chunk_size, total)
        ser.write(raw_padded[sent:end])
        ser.flush()
        sent = end
        time.sleep(0.002)
    time.sleep(0.05)

def send_label_image_to_printer(ser_yazici: Optional[serial.Serial], payload: Dict[str, Any], feed_after_lines: int, preview_only: bool, on_preview_image=None):
    img = compose_label(payload, WIDTH_DOTS, HEIGHT_DOTS, BOTTOM_FORBID)

    # Fiziksel aşağı indirme (mm → dot). ROTATE_180=True iken fiziksel aşağı = dy negatif.
    if PHYS_SHIFT_DOWN_MM != 0:
        dy = (-mm_to_dots(PHYS_SHIFT_DOWN_MM)) if ROTATE_180 else (mm_to_dots(PHYS_SHIFT_DOWN_MM))
        img = shift_image_vertical(img, dy=dy, fill=(255, 255, 255))

    # Raster’a çevir
    if img.size != (WIDTH_DOTS, HEIGHT_DOTS):
        img = img.resize((WIDTH_DOTS, HEIGHT_DOTS), Image.LANCZOS)
    raw_label, label_wb, rows = to_1bit_bytes(img, WIDTH_DOTS)

    # Yatay sola kaydırma (merkezden)
    raw_padded = pad_rows_to_device_width(
        raw_label, label_wb=label_wb, device_wb=DEVICE_WIDTH_BYTES, rows=rows,
        align="center", left_shift_dots=mm_to_dots(H_SHIFT_MM)
    )

    # Önizleme artefaktları
    try:
        img.save(PREVIEW_PNG_PATH)
        img.convert("1").save(PREVIEW_BMP1_PATH, format="BMP")
        with open(PREVIEW_BIN_PATH, "wb") as f:
            f.write(raw_padded)
    except Exception:
        pass
    if callable(on_preview_image):
        on_preview_image(img)

    if preview_only or ser_yazici is None:
        return

    clear_printer_buffer(ser_yazici)
    send_single_esc_v_height_only(ser_yazici, raw_padded, rows=rows)

    if feed_after_lines > 0:
        ser_yazici.write(b"\n" * feed_after_lines)
        ser_yazici.flush()
    time.sleep(0.2)

# =========================
# Terazi / AD2K
# =========================
def make_ad2k_frame(command_bytes):
    frame = b'\x02' + command_bytes + b'\x03'
    bcc = 0
    for b in frame:
        bcc ^= b
    return frame + bytes([bcc])

def send_ad2k_command(ser, command_bytes, response_timeout=0.6):
    try:
        ser.reset_input_buffer()
    except Exception:
        pass
    try:
        ser.write(b'\x13')  # Xoff
    except Exception:
        return b""
    time.sleep(0.02)
    frame = make_ad2k_frame(command_bytes)
    ser.write(frame)
    time.sleep(0.02)
    ser.write(b'\x11')  # Xon
    ser.flush()
    resp = b""
    start = time.time()
    while time.time() - start < response_timeout:
        chunk = ser.read(ser.in_waiting or 1)
        if chunk:
            resp += chunk
        else:
            time.sleep(0.01)
    return resp

def parse_weight_line(line):
    if isinstance(line, bytes):
        line = line.decode(errors="ignore")
    s = (line or "").strip()

    # Bilinen hatalı değer
    if re.search(r'\b400000(?:[.,]00)?\s*g\b', s, re.IGNORECASE):
        return None

    # 1) "ST,GS, 0.123 kg" vb.
    m = re.search(r'(?:ST|US|OL)?\s*,?\s*(?:GS|NT|TR)?\s*,?\s*([-+]?\d+(?:[.,]\d+)?)\s*(kg|g)\b', s, re.IGNORECASE)
    if m:
        val = m.group(1).replace(",", ".")
        unit = m.group(2).lower()
        try:
            v = float(val)
            grams = int(round(v * 1000)) if unit == "kg" else int(round(v))
            if abs(grams) < 5 or abs(grams) > MAX_REALISTIC_GRAMS:
                return None
            return grams
        except Exception:
            pass

    # 2) “0,123 kg” / “2.500 kg” / “123 g”
    m = re.search(r'([-+]?\d+(?:[.,]\d+)?)\s*(kg|g)\b', s, re.IGNORECASE)
    if m:
        val = m.group(1).replace(",", ".")
        unit = m.group(2).lower()
        try:
            v = float(val)
            grams = int(round(v * 1000)) if unit == "kg" else int(round(v))
            if abs(grams) < 5 or abs(grams) > MAX_REALISTIC_GRAMS:
                return None
            return grams
        except Exception:
            pass

    # 3) “12,345” -> 12kg 345g
    m = re.search(r'(?<!\d)(\d+),(\d{1,3})(?!\d)', s)
    if m:
        try:
            whole = int(m.group(1))
            frac = m.group(2)
            while len(frac) < 3:
                frac += "0"
            frac = frac[:3]
            grams = whole * 1000 + int(frac)
            if grams < 5 or grams > MAX_REALISTIC_GRAMS:
                return None
            return grams
        except Exception:
            pass

    # 4) Eski kalıp
    m = re.search(r'\b0000(\d),(\d{3})', s)
    if m:
        kg = int(m.group(1)); gr = int(m.group(2))
        grams = kg * 1000 + gr
        if grams < 5 or grams > MAX_REALISTIC_GRAMS:
            return None
        return grams

    # 5) yalın “123 g”
    m = re.search(r'(?<!\d)(-?\d+)\s*g\b', s, re.IGNORECASE)
    if m:
        grams = int(m.group(1))
        if abs(grams) < 5 or abs(grams) > MAX_REALISTIC_GRAMS:
            return None
        return grams

    return None

def stable_value(stable_queue: deque, tolerance: int) -> bool:
    if len(stable_queue) < stable_queue.maxlen:
        return False
    return (max(stable_queue) - min(stable_queue)) <= tolerance

# =========================
# Port Keşfi
# =========================
def _port_matches(tokens: List[str], info) -> bool:
    low_fields = " ".join([
        str(info.device or ""),
        str(info.name or ""),
        str(info.description or ""),
        str(info.manufacturer or ""),
        str(info.hwid or ""),
        str(info.interface or ""),
        str(info.serial_number or ""),
    ]).lower()
    return any(tok in low_fields for tok in tokens)

def auto_serial_port_terazi() -> Optional[str]:
    env = os.getenv("TERAZI_PORT")
    if env:
        return env
    try:
        ports = list(list_ports.comports())
    except Exception:
        ports = []
    if IS_WINDOWS and ports:
        for p in ports:
            if str(p.device).upper() == "COM6":
                return "COM6"
    if not ports:
        return SCL_PORT_FALLBACK
    tokens_primary = ["ftdi", "ad", "terazi", "scale", "weigh"]
    tokens_secondary = ["usb", "serial", "com"]
    for p in ports:
        if _port_matches(tokens_primary, p):
            return p.device
    for p in ports:
        if _port_matches(tokens_secondary, p):
            return p.device
    return ports[0].device if ports else SCL_PORT_FALLBACK

def auto_serial_port_yazici() -> str:
    env = os.getenv("YAZICI_PORT") or os.getenv("PRINTER_PORT")
    if env:
        return env
    ports = list(list_ports.comports())
    tokens_primary = ["topway", "printer", "yazici", "label", "usb-serial", "usb serial"]
    tokens_secondary = ["usb", "serial", "com"]
    for p in ports:
        if _port_matches(tokens_primary, p):
            return p.device
    for p in ports:
        if _port_matches(tokens_secondary, p):
            return p.device
    return PRN_PORT_FALLBACK

# =========================
# GUI Uygulaması
# =========================
class LabelApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Terazi Etiket Yazıcı")
        self.geometry("1120x820")
        self.minsize(980, 700)

        # Paylaşılan durum
        self.stop_event = threading.Event()
        self.log_q: queue.Queue[str] = queue.Queue()
        self.raw_q: queue.Queue[str] = queue.Queue()

        self.ser_terazi: Optional[serial.Serial] = None
        self.ser_yazici: Optional[serial.Serial] = None

        self.current_mrp_id: Optional[Any] = None
        self.sending_data_remote = False  # Odoo 'start/done'
        self.sending_data_local = False   # GUI Start/Done
        self.print_single_mode = False    # Odoo 'print_single'
        self.preview_only = tk.BooleanVar(value=False)

        self.last_action_id: Optional[str] = None
        self.processed_series_tokens: set[str] = set()
        self.MAX_TOKEN_CACHE = 200

        # Tartı durumu
        self.stable_queue: deque[int] = deque(maxlen=STABLE_COUNT)
        self.last_printed_weight: Optional[int] = None
        self.sent_last_weight: Optional[int] = None
        self.weight_var = tk.StringVar(value="0 g")
        self.weight_kg_var = tk.StringVar(value="0.000 kg")
        self.stable_var = tk.StringVar(value="Kararsız")
        self.job_status_var = tk.StringVar(value="Bekleniyor...")
        self.scale_port_var = tk.StringVar(value="(yok)")
        self.printer_port_var = tk.StringVar(value="(yok)")

        # Terazi seri ayarları
        self.serial_baud_var = tk.StringVar(value=str(SCL_BAUD))
        self.serial_parity_var = tk.StringVar(value="ODD")  # NONE/EVEN/ODD
        self.xonxoff_var = tk.BooleanVar(value=False)
        self.poll_mode = tk.BooleanVar(value=True)          # True: komutla poll, False: ham dinle
        self.show_raw = tk.BooleanVar(value=True)           # Ham veri penceresi

        # Fiziksel ayarların canlı kontrolü
        self.vert_mm_var = tk.DoubleVar(value=PHYS_SHIFT_DOWN_MM)
        self.horz_mm_var = tk.DoubleVar(value=H_SHIFT_MM)

        # Önizleme
        self.preview_canvas = None
        self.preview_photo = None

        self._build_ui()
        self._auto_connect()

        self.job_thread = threading.Thread(target=self._job_worker, name="JobWorker", daemon=True)
        self.scale_thread = threading.Thread(target=self._scale_worker, name="ScaleWorker", daemon=True)
        self.job_thread.start()
        self.scale_thread.start()

        # Başlangıç log
        self._log(f"Sans Serif -> normal: {SANS_NORMAL_PATH or '(yok)'} | bold: {SANS_BOLD_PATH or '(yok)'} | FORCE_SANS_SERIF={FORCE_SANS_SERIF}")
        self._log(f"Fiziksel ofset başlangıç: aşağı={self.vert_mm_var.get()} mm, sola={self.horz_mm_var.get()} mm")

        self.after(100, self._gui_pulse)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI ----------
    def _build_ui(self):
        pad = 8

        top = ttk.Frame(self)
        top.pack(fill="x", padx=pad, pady=pad)

        ttk.Label(top, text="Terazi Port:").grid(row=0, column=0, sticky="w")
        ttk.Label(top, textvariable=self.scale_port_var).grid(row=0, column=1, sticky="w", padx=(4,16))

        ttk.Label(top, text="Yazıcı Port:").grid(row=0, column=2, sticky="w")
        ttk.Label(top, textvariable=self.printer_port_var).grid(row=0, column=3, sticky="w", padx=(4,16))

        ttk.Button(top, text="Portları Yenile", command=self._refresh_ports).grid(row=0, column=4, padx=4)
        ttk.Button(top, text="Yeniden Bağlan", command=self._reconnect_ports).grid(row=0, column=5, padx=4)

        settings = ttk.Frame(self)
        settings.pack(fill="x", padx=pad, pady=(0, pad))
        ttk.Label(settings, text="Baud:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(settings, width=8, textvariable=self.serial_baud_var, values=["4800","9600","19200","38400"]).grid(row=0, column=1, padx=(2,12))
        ttk.Label(settings, text="Parity:").grid(row=0, column=2, sticky="w")
        ttk.Combobox(settings, width=6, textvariable=self.serial_parity_var, values=["NONE","EVEN","ODD"]).grid(row=0, column=3, padx=(2,12))
        ttk.Checkbutton(settings, text="XON/XOFF", variable=self.xonxoff_var).grid(row=0, column=4, padx=(2,12))
        ttk.Button(settings, text="Ayarları Uygula ve Bağlan", command=self._reconnect_ports).grid(row=0, column=5, padx=(2,12))

        ttk.Label(settings, text="Okuma Modu:").grid(row=0, column=6, sticky="e", padx=(24,4))
        ttk.Radiobutton(settings, text="POLL (Komutla)", variable=self.poll_mode, value=True).grid(row=0, column=7, sticky="w")
        ttk.Radiobutton(settings, text="LISTEN (Ham Dinle)", variable=self.poll_mode, value=False).grid(row=0, column=8, sticky="w", padx=(4,0))
        ttk.Checkbutton(settings, text="Ham Veriyi Göster", variable=self.show_raw).grid(row=0, column=9, padx=(16,0))

        # Konum kalibrasyonu paneli
        cal = ttk.LabelFrame(self, text="Fiziksel Konum Kalibrasyonu (mm)")
        cal.pack(fill="x", padx=pad, pady=(0, pad))
        ttk.Label(cal, text="Aşağı (+mm):").grid(row=0, column=0, sticky="e")
        ttk.Spinbox(cal, from_=0, to=30, increment=0.5, textvariable=self.vert_mm_var, width=6).grid(row=0, column=1, padx=(4,16))
        ttk.Label(cal, text="Sola (+mm):").grid(row=0, column=2, sticky="e")
        ttk.Spinbox(cal, from_=0, to=30, increment=0.5, textvariable=self.horz_mm_var, width=6).grid(row=0, column=3, padx=(4,16))
        ttk.Button(cal, text="Uygula", command=self._apply_physical_shifts).grid(row=0, column=4, padx=(4,12))

        # Hızlı nudge
        ttk.Button(cal, text="Aşağı +0.5", command=lambda: self._nudge(0.5, 0)).grid(row=0, column=5, padx=2)
        ttk.Button(cal, text="Yukarı -0.5", command=lambda: self._nudge(-0.5, 0)).grid(row=0, column=6, padx=2)
        ttk.Button(cal, text="Sola +0.5", command=lambda: self._nudge(0, 0.5)).grid(row=0, column=7, padx=2)
        ttk.Button(cal, text="Sağa -0.5", command=lambda: self._nudge(0, -0.5)).grid(row=0, column=8, padx=2)

        mid = ttk.Frame(self)
        mid.pack(fill="x", padx=pad)

        weight_frame = ttk.LabelFrame(mid, text="Ağırlık")
        weight_frame.pack(side="left", fill="both", expand=True, padx=(0, pad), pady=(0, pad))

        big = ttk.Frame(weight_frame)
        big.pack(fill="x", padx=pad, pady=pad)

        self.weight_label = ttk.Label(big, textvariable=self.weight_var, font=("Segoe UI", 36, "bold"))
        self.weight_label.pack(side="left")

        self.stable_label = ttk.Label(big, textvariable=self.stable_var, foreground="red", font=("Segoe UI", 12, "bold"))
        self.stable_label.pack(side="left", padx=12)

        ttk.Label(weight_frame, textvariable=self.weight_kg_var, font=("Segoe UI", 16)).pack(anchor="w", padx=pad)

        status_frame = ttk.Frame(weight_frame)
        status_frame.pack(fill="x", padx=pad, pady=(4, pad))
        ttk.Label(status_frame, text="Durum: ").pack(side="left")
        ttk.Label(status_frame, textvariable=self.job_status_var, font=("Segoe UI", 10, "italic")).pack(side="left")

        ctrl = ttk.LabelFrame(mid, text="Kontroller")
        ctrl.pack(side="left", fill="y", padx=(0, pad), pady=(0, pad))
        ttk.Button(ctrl, text="Dara (Tare)", command=self._do_tare, width=16).pack(padx=pad, pady=4)
        ttk.Button(ctrl, text="Sıfır (Zero)", command=self._do_zero, width=16).pack(padx=pad, pady=4)
        ttk.Separator(ctrl, orient="horizontal").pack(fill="x", padx=pad, pady=6)
        ttk.Button(ctrl, text="Start (Yerel)", command=self._local_start, width=16).pack(padx=pad, pady=4)
        ttk.Button(ctrl, text="Done (Yerel)", command=self._local_done, width=16).pack(padx=pad, pady=4)
        ttk.Checkbutton(ctrl, text="Preview Only", variable=self.preview_only).pack(padx=pad, pady=6)
        ttk.Button(ctrl, text="3 sn Ham Oku", command=self._read_raw_3s).pack(padx=pad, pady=6)

        right = ttk.LabelFrame(self, text="Önizleme ve Kayıtlar")
        right.pack(fill="both", expand=True, padx=pad, pady=(0, pad))

        self.preview_canvas = tk.Canvas(right, width=380, height=380, bg="#f2f2f2", highlightthickness=1, relief="sunken")
        self.preview_canvas.pack(side="left", padx=pad, pady=pad)

        nb = ttk.Notebook(right)
        nb.pack(side="left", fill="both", expand=True, padx=(0, pad), pady=pad)

        log_tab = ttk.Frame(nb)
        raw_tab = ttk.Frame(nb)
        nb.add(log_tab, text="Günlük")
        nb.add(raw_tab, text="Ham Tartı")

        self.log_text = tk.Text(log_tab, height=14, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)
        ttk.Button(log_tab, text="Günlüğü Temizle", command=self._clear_log).pack(anchor="e", padx=pad, pady=(4,0))

        self.raw_text = tk.Text(raw_tab, height=14, wrap="none", state="disabled")
        self.raw_text.pack(fill="both", expand=True)
        ttk.Button(raw_tab, text="Ham Veriyi Temizle", command=self._clear_raw).pack(anchor="e", padx=pad, pady=(4,0))

    # ---------- Bağlantı ----------
    def _refresh_ports(self):
        scale_guess = auto_serial_port_terazi()
        prn_guess = auto_serial_port_yazici()
        self.scale_port_var.set(scale_guess or "(yok)")
        self.printer_port_var.set(prn_guess or "(yok)")
        self._log(f"Port keşfi -> Terazi: {scale_guess} | Yazıcı: {prn_guess}")

    def _auto_connect(self):
        self._refresh_ports()
        self._reconnect_ports()

    def _map_parity(self, name: str):
        name = (name or "").upper()
        if name == "NONE":
            return serial.PARITY_NONE
        if name == "EVEN":
            return serial.PARITY_EVEN
        return serial.PARITY_ODD

    def _reconnect_ports(self):
        # Terazi
        scl = self.scale_port_var.get()
        if scl and scl != "(yok)":
            try:
                if self.ser_terazi and self.ser_terazi.is_open:
                    self.ser_terazi.close()
                self.ser_terazi = serial.Serial(
                    port=scl,
                    baudrate=int(self.serial_baud_var.get() or SCL_BAUD),
                    bytesize=serial.EIGHTBITS,
                    parity=self._map_parity(self.serial_parity_var.get() or "ODD"),
                    stopbits=serial.STOPBITS_ONE,
                    timeout=SCL_TIMEOUT,
                    xonxoff=self.xonxoff_var.get(),
                )
                time.sleep(0.15)
                self._log(f"Terazi bağlandı: {scl} (baud={self.ser_terazi.baudrate}, parity={self.serial_parity_var.get()}, xonxoff={self.xonxoff_var.get()}, mode={'POLL' if self.poll_mode.get() else 'LISTEN'})")
            except Exception as e:
                self._log(f"Terazi bağlanamadı ({scl}): {e}")

        # Yazıcı
        prn = self.printer_port_var.get()
        if prn and prn != "(yok)":
            try:
                if self.ser_yazici and self.ser_yazici.is_open:
                    self.ser_yazici.close()
                self.ser_yazici = serial.Serial(
                    port=prn, baudrate=PRN_BAUD, bytesize=serial.EIGHTBITS,
                    parity=PRN_PARITY, stopbits=serial.STOPBITS_ONE, timeout=PRN_TIMEOUT,
                )
                time.sleep(0.1)
                printer_handshake(self.ser_yazici)
                self._log(f"Yazıcı bağlandı: {prn}")
            except Exception as e:
                self._log(f"Yazıcı bağlanamadı ({prn}): {e}")

    # ---------- Fiziksel konum uygulama ----------
    def _apply_physical_shifts(self):
        global PHYS_SHIFT_DOWN_MM, H_SHIFT_MM
        PHYS_SHIFT_DOWN_MM = max(0.0, float(self.vert_mm_var.get()))
        H_SHIFT_MM = max(0.0, float(self.horz_mm_var.get()))
        self._log(f"Fiziksel ofset uygulandı: aşağı={PHYS_SHIFT_DOWN_MM:.2f} mm, sola={H_SHIFT_MM:.2f} mm")

    def _nudge(self, d_down_mm: float, d_left_mm: float):
        self.vert_mm_var.set(max(0.0, self.vert_mm_var.get() + d_down_mm))
        self.horz_mm_var.set(max(0.0, self.horz_mm_var.get() + d_left_mm))
        self._apply_physical_shifts()

    # ---------- GUI olayları ----------
    def _do_tare(self):
        if self.ser_terazi and self.ser_terazi.is_open:
            try:
                _ = send_ad2k_command(self.ser_terazi, b'T')
                self._log("DARA komutu gönderildi.")
            except Exception as e:
                self._log(f"DARA hata: {e}")

    def _do_zero(self):
        if self.ser_terazi and self.ser_terazi.is_open:
            try:
                _ = send_ad2k_command(self.ser_terazi, b'Z')
                self._log("SIFIR komutu gönderildi.")
            except Exception as e:
                self._log(f"SIFIR hata: {e}")

    def _local_start(self):
        self.sending_data_local = True
        self.job_status_var.set("Yerel START aktif (Odoo ile birlikte)")
        self._log("Yerel START etkin.")

    def _local_done(self):
        self.sending_data_local = False
        self.job_status_var.set("Yerel DONE (akış durdu)")
        self._log("Yerel DONE gönderildi, yerel akış durdu.")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _clear_raw(self):
        self.raw_text.configure(state="normal")
        self.raw_text.delete("1.0", "end")
        self.raw_text.configure(state="disabled")

    # ---------- İş parçacıkları ----------
    def _job_worker(self):
        while not self.stop_event.is_set():
            try:
                job = self._fetch_job()
                job_str = (job.get("job") or "").lower()
                mrp_id = job.get("mrp_id")
                action_id = json.dumps(job, sort_keys=True)

                if job_str and action_id != self.last_action_id:
                    if job_str == "start":
                        self.print_single_mode = bool(job.get("print_single", False))
                        self._set_remote_stream(True, mrp_id)
                        self.stable_queue.clear()
                        self.sent_last_weight = None
                        self._log(f"Odoo START: print_single={self.print_single_mode}")
                        self.last_action_id = action_id

                    elif job_str == "done":
                        self._set_remote_stream(False, mrp_id=None)
                        self._log("Odoo DONE: Tartı akışı kapatıldı.")
                        self.last_action_id = action_id

                    elif job_str == "tare":
                        if self.ser_terazi and self.ser_terazi.is_open:
                            try:
                                _ = send_ad2k_command(self.ser_terazi, b'T')
                                self._log("Odoo TARE: Dara gönderildi.")
                            except Exception as e:
                                self._log(f"Odoo TARE hata: {e}")
                        self.last_action_id = action_id

                    elif job_str == "zero":
                        if self.ser_terazi and self.ser_terazi.is_open:
                            try:
                                _ = send_ad2k_command(self.ser_terazi, b'Z')
                                self._log("Odoo ZERO: Sıfır gönderildi.")
                            except Exception as e:
                                self._log(f"Odoo ZERO hata: {e}")
                        self.last_action_id = action_id

                    elif job_str in ("print_series", "print_n", "print_fixed"):
                        token = self._get_job_token(job)
                        if token in self.processed_series_tokens:
                            self._log(f"Aynı create_date'li seri iş zaten işlendi (token={token}), atlandı.")
                            self.last_action_id = action_id
                            continue

                        copies = int(job.get("copies") or 1)
                        delay_sec = int(job.get("delay_sec") or 5)
                        fixed_weight = int(job.get("weight") or 0)
                        payload_override = job.get("payload") or {}
                        if isinstance(payload_override, str):
                            try:
                                payload_override = json.loads(payload_override)
                            except Exception:
                                payload_override = {}

                        payload_from_odoo, resp_copies = self._fetch_label_payload_from_odoo(mrp_id, fixed_weight)
                        if payload_from_odoo is None:
                            self._log("Odoo payload alınamadı; seri baskı atlandı.")
                            self.last_action_id = action_id
                            continue

                        payload = {**payload_from_odoo, **payload_override}
                        if FORCE_SANS_SERIF and not payload.get("font_path"):
                            payload["font_path"] = SANS_NORMAL_PATH

                        eff_copies = copies if copies > 0 else self._compute_copies({}, resp_copies, payload)
                        eff_copies = max(1, eff_copies)

                        self._log(f"PRINT_SERIES: mrp_id={mrp_id}, copies={eff_copies}, delay={delay_sec}s, weight={fixed_weight}")
                        for i in range(eff_copies):
                            self._send_label(payload)
                            self._log(f" -> {i+1}/{eff_copies} basıldı")
                            if i < eff_copies - 1:
                                for _ in range(delay_sec * 10):
                                    if self.stop_event.is_set():
                                        break
                                    time.sleep(0.1)

                        self.processed_series_tokens.add(token)
                        if len(self.processed_series_tokens) > self.MAX_TOKEN_CACHE:
                            self.processed_series_tokens = set(list(self.processed_series_tokens)[-self.MAX_TOKEN_CACHE:])
                        self.last_action_id = action_id
            except Exception as e:
                self._log(f"JobWorker hata: {e}")
            for _ in range(5):
                if self.stop_event.is_set():
                    break
                time.sleep(0.05)

    def _scale_worker(self):
        buffer = b""
        while not self.stop_event.is_set():
            try:
                if not (self.ser_terazi and self.ser_terazi.is_open):
                    time.sleep(0.2)
                    continue

                if self.poll_mode.get():
                    resp = send_ad2k_command(self.ser_terazi, b'RN\x1C', response_timeout=0.4)
                    if resp:
                        self._push_raw(resp)
                    buffer += resp
                    extra = self.ser_terazi.read(self.ser_terazi.in_waiting or 0)
                    if extra:
                        self._push_raw(extra)
                        buffer += extra
                else:
                    chunk = self.ser_terazi.read(128)
                    if chunk:
                        self._push_raw(chunk)
                        buffer += chunk

                while b"\r" in buffer or b"\n" in buffer:
                    sep = b"\r" if b"\r" in buffer else b"\n"
                    line, buffer = buffer.split(sep, 1)
                    if not line:
                        continue

                    weight = parse_weight_line(line)
                    if weight is None:
                        continue

                    self._update_weight_display(weight)
                    self.stable_queue.append(weight)
                    is_stable = stable_value(self.stable_queue, SENSITIVITY_GRAM)
                    self._set_stable(is_stable)

                    if not self._effective_sending():
                        continue
                    mrp_id = self.current_mrp_id
                    if not mrp_id or not is_stable:
                        continue
                    if self.sent_last_weight is not None and abs(self.sent_last_weight - weight) < SENSITIVITY_GRAM:
                        continue

                    payload_from_odoo, resp_copies = self._fetch_label_payload_from_odoo(mrp_id, weight)
                    if payload_from_odoo is None:
                        self._log("Odoo payload alınamadı; baskı atlandı.")
                        self.stable_queue.clear()
                        self.sent_last_weight = weight
                        continue

                    payload = dict(payload_from_odoo)
                    if FORCE_SANS_SERIF and not payload.get("font_path"):
                        payload["font_path"] = SANS_NORMAL_PATH
                    if not payload.get("product_name"):
                        payload["product_name"] = ""
                    if not payload.get("weight_str"):
                        payload["weight_str"] = f"{weight/1000.0:.3f} KG"

                    copies_to_print = 1 if self.print_single_mode else self._compute_copies({}, resp_copies, payload)
                    copies_to_print = max(1, copies_to_print)

                    for i in range(copies_to_print):
                        self._send_label(payload)
                        self._log(f"Baskı OK ({i+1}/{copies_to_print}) – {weight} g")
                        self.last_printed_weight = weight

                    self.stable_queue.clear()
                    self.sent_last_weight = weight

                    if self.print_single_mode:
                        self.sending_data_remote = False
                        self.sending_data_local = False
                        self.job_status_var.set("Tek baskı tamamlandı, akış kapatıldı.")
            except Exception as e:
                self._log(f"ScaleWorker hata: {e}")
                time.sleep(0.2)

    # ---------- Yardımcılar ----------
    def _send_label(self, payload: Dict[str, Any]):
        try:
            send_label_image_to_printer(
                self.ser_yazici if (self.ser_yazici and self.ser_yazici.is_open) else None,
                payload,
                feed_after_lines=FEED_AFTER_LINES,
                preview_only=self.preview_only.get(),
                on_preview_image=self._update_preview_image,
            )
        except Exception as e:
            self._log(f"Baskı hatası: {e}")

    def _update_preview_image(self, pil_img: Image.Image):
        if not self.preview_canvas:
            return
        c_w = int(self.preview_canvas["width"])
        c_h = int(self.preview_canvas["height"])
        img = pil_img.copy()
        img.thumbnail((c_w, c_h), Image.LANCZOS)
        self.preview_photo = ImageTk.PhotoImage(img)
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(c_w//2, c_h//2, image=self.preview_photo)

    def _update_weight_display(self, grams: int):
        self.weight_var.set(f"{grams} g")
        self.weight_kg_var.set(f"{grams/1000.0:.3f} kg")

    def _set_stable(self, is_stable: bool):
        if is_stable:
            self.stable_var.set("Stabil")
            self.stable_label.configure(foreground="green")
        else:
            self.stable_var.set("Kararsız")
            self.stable_label.configure(foreground="red")

    def _effective_sending(self) -> bool:
        return self.sending_data_remote or self.sending_data_local

    def _set_remote_stream(self, enabled: bool, mrp_id: Optional[Any]):
        self.sending_data_remote = enabled
        if enabled:
            self.current_mrp_id = mrp_id
            self.job_status_var.set(f"Odoo START – mrp_id={mrp_id}")
        else:
            self.job_status_var.set("Odoo DONE")

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        try:
            self.log_q.put_nowait(f"[{ts}] {msg}")
        except Exception:
            pass

    def _push_raw(self, data: bytes):
        if not self.show_raw.get() or not data:
            return
        try:
            s = data.decode(errors="ignore")
        except Exception:
            s = repr(data)
        for part in re.split(r'[\r\n]+', s):
            if part:
                try:
                    self.raw_q.put_nowait(part)
                except Exception:
                    pass

    def _read_raw_3s(self):
        if not (self.ser_terazi and self.ser_terazi.is_open):
            self._log("Ham okuma: Terazi bağlı değil.")
            return
        def run():
            self._log("Ham okuma başlatıldı (3 sn).")
            end = time.time() + 3.0
            while time.time() < end and not self.stop_event.is_set():
                try:
                    chunk = self.ser_terazi.read(256)
                    if chunk:
                        self._push_raw(chunk)
                except Exception as e:
                    self._log(f"Ham okuma hata: {e}")
                    break
                time.sleep(0.01)
            self._log("Ham okuma bitti.")
        threading.Thread(target=run, daemon=True).start()

    def _gui_pulse(self):
        # Günlük
        while True:
            try:
                line = self.log_q.get_nowait()
            except queue.Empty:
                break
            else:
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")

        # Ham veri
        while True:
            try:
                raw = self.raw_q.get_nowait()
            except queue.Empty:
                break
            else:
                self.raw_text.configure(state="normal")
                self.raw_text.insert("end", raw + "\n")
                self.raw_text.see("end")
                self.raw_text.configure(state="disabled")

        self.after(100, self._gui_pulse)

    def _on_close(self):
        if messagebox.askokcancel("Çıkış", "Uygulamadan çıkılsın mı?"):
            self.stop_event.set()
            try:
                if self.ser_terazi and self.ser_terazi.is_open:
                    self.ser_terazi.close()
            except Exception:
                pass
            try:
                if self.ser_yazici and self.ser_yazici.is_open:
                    self.ser_yazici.close()
            except Exception:
                pass
            self.destroy()

    # ---------- Ağ ve iş mantığı ----------
    def _fetch_job(self) -> Dict[str, Any]:
        try:
            resp = requests.get(GET_JOB_URL, timeout=4)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    return data[0]
                if isinstance(data, dict):
                    return data
        except Exception as e:
            self._log(f"Odoo iş çekme hatası: {e}")
        return {"job": "", "mrp_id": None}

    def _fetch_label_payload_from_odoo(self, mrp_id: Any, weight_grams: int) -> Tuple[Optional[Dict[str, Any]], int]:
        try:
            url = ODOO_URL_TEMPLATE.format(mrp_id=mrp_id, weight=weight_grams)
            r = requests.get(url, timeout=6)
            if r.status_code != 200:
                self._log(f"Label fetch HTTP: {r.status_code} {r.text[:120]}")
                return None, 1
            data = r.json()
            if isinstance(data, dict) and "label" in data:
                payload = data.get("label") or {}
                copies = int(data.get("copies") or 1)
                return payload, copies
            return data, int(data.get("copies") or 1) if isinstance(data, dict) else 1
        except Exception as e:
            self._log(f"Label fetch/parse error: {e}")
            return None, 1

    @staticmethod
    def _compute_copies(job: Dict[str, Any], resp_copies: int, payload: Dict[str, Any]) -> int:
        if isinstance(job.get("copies"), (int, float)) and int(job["copies"]) > 0:
            return int(job["copies"])
        if isinstance(resp_copies, int) and resp_copies > 0:
            return resp_copies
        cnt = payload.get("count")
        try:
            n = int(str(cnt).strip())
            if n > 0:
                return n
        except Exception:
            pass
        return 1

    @staticmethod
    def _get_job_token(job: Dict[str, Any]) -> str:
        return f"{job.get('job','')}|{job.get('mrp_id')}|{job.get('create_date','')}"

# =========================
# Çalıştırma
# =========================
if __name__ == "__main__":
    app = LabelApp()
    app.mainloop()