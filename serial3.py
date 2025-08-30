from __future__ import annotations

# GUI'li birleşik sürüm (Windows uyumlu, Tkinter arayüzlü)
# - Odoo komut akışını ve baskı işleyişini korur.
# - Tartıdan gelen anlık ağırlığı GUI'de gösterir, kararlılık durumunu işaretler.
# - Odoo "start" ile stabilize olunca otomatik baskı; "print_series" akışını aynen uygular.
# - Tare/Zero tuşları GUI üzerinden de gönderilebilir.
# - Etkileşim: Preview Only (yazıcıya göndermeden önizleme üret), Start/Done (yerel),
#   Portları yenile/yeniden bağlan (Windows COM ve diğer platformlar).
#
# Gereksinimler: pip install pillow pyserial requests
#
# Not: Bu dosya tek başına çalışır; Windows ve Linux/Mac için port keşfi içerir.

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
GET_JOB_URL = "https://altinayet-stage-22335048.dev.odoo.com/terazi/get_scale_job/1"   # scale_id'yi güncelle
ODOO_URL_TEMPLATE = "https://altinayet-stage-22335048.dev.odoo.com/terazi/get/{mrp_id}/{weight}"

STABLE_COUNT = 5
SENSITIVITY_GRAM = 20

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

DEVICE_WIDTH_BYTES = 108
DEVICE_WIDTH_DOTS  = DEVICE_WIDTH_BYTES * 8  # 864

DATA_CHUNK_SIZE = 4096
FEED_AFTER_LINES = 1  # 0..2 önerilir

# Tuval ve işleme
REQ_W = 748
REQ_H = 748
BOTTOM_FORBID = 120
ROTATE_180 = True
THRESHOLD = 192
INVERT_BW = False

# Yerleşim
TITLE_Y = 60
LEFT_BLOCK_Y = 150
LEFT_BLOCK_GAP = 44
LEFT_MARGIN = 32
LEFT_COL_WIDTH = 300
COL_GAP = 20
RIGHT_BARCODE_HEIGHT = 120

# Font adayları
DEFAULT_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
]
def get_default_font_path() -> Optional[str]:
    for p in DEFAULT_FONT_CANDIDATES:
        try:
            if os.path.exists(p):
                return p
        except Exception:
            pass
    return None
DEFAULT_FONT_PATH = get_default_font_path()

# =========================
# Terazi (AD2K) Ayarları
# =========================
SCL_BAUD = 19200
SCL_PARITY = serial.PARITY_ODD
SCL_TIMEOUT = 0.5

# =========================
# Görsel/Raster Yardımcıları
# =========================
def round_to_8(n: int) -> int:
    return int(math.ceil(n / 8.0) * 8)

WIDTH_DOTS = round_to_8(REQ_W)   # 752
HEIGHT_DOTS = REQ_H
LABEL_WIDTH_BYTES = WIDTH_DOTS // 8  # 94

def load_font(font_path: str | None, size: int) -> ImageFont.ImageFont:
    try_paths: List[Optional[str]] = []
    if font_path:
        try_paths.append(font_path)
    try_paths.extend(DEFAULT_FONT_CANDIDATES)
    for p in try_paths:
        if not p:
            continue
        try:
            if os.path.exists(p):
                return ImageFont.truetype(p, size=size)
        except Exception:
            continue
    return ImageFont.load_default()

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
            candidate = w if not buf else buf + " " + w
            if draw.textlength(candidate, font=font) <= max_width:
                buf = candidate
            else:
                if buf:
                    lines.append(buf)
                buf = w
        if buf:
            lines.append(buf)
    return "\n".join(lines)

# EAN-13 tabloları
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
    digits = "".join(ch for ch in data if ch.isdigit())
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

def compose_label(data: Dict[str, Any], width_dots: int, height_dots: int, forbid_bottom_px: int) -> Image.Image:
    font_path = data.get("font_path") or DEFAULT_FONT_PATH
    canvas = Image.new("RGB", (width_dots, height_dots), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    f_title = load_font(font_path, 44)
    f_sub   = load_font(font_path, 26)
    f_label = load_font(font_path, 24)
    f_text  = load_font(font_path, 20)
    f_bar   = load_font(font_path, 20)

    product = str(data.get("product_name", "")).strip()
    if product:
        tw = draw.textlength(product, font=f_title)
        draw.text(((width_dots - tw)//2, TITLE_Y), product, font=f_title, fill=(0,0,0))

    y0 = LEFT_BLOCK_Y
    left_x = LEFT_MARGIN
    label_w = 120
    val_x = left_x + label_w + 8

    draw.text((left_x, y0), "Adet:", font=f_label, fill=(0,0,0))
    draw.text((val_x,  y0), str(data.get("count", "")), font=f_label, fill=(0,0,0))

    y1 = y0 + LEFT_BLOCK_GAP
    draw.text((left_x, y1), "Ağırlık:", font=f_label, fill=(0,0,0))
    draw.text((val_x,  y1), str(data.get("weight_str", "")), font=f_sub, fill=(0,0,0))

    y2 = y1 + LEFT_BLOCK_GAP
    draw.text((left_x, y2), "S.T.T.:", font=f_label, fill=(0,0,0))
    draw.text((val_x,  y2), str(data.get("expiry", "")), font=f_label, fill=(0,0,0))

    right_x = LEFT_MARGIN + LEFT_COL_WIDTH + COL_GAP
    right_w = max(200, width_dots - right_x - LEFT_MARGIN)
    bar_x = right_x
    bar_top = y0
    bar_h = RIGHT_BARCODE_HEIGHT
    draw_ean13(canvas, bar_x, bar_top, right_w, bar_h, str(data.get("barcode", "")), f_bar)

    last_left_y = y2 + f_label.size
    last_barcode_y = bar_top + bar_h + max(12, int(RIGHT_BARCODE_HEIGHT * 0.18)) + 4
    text_top = max(last_left_y, last_barcode_y) + 16

    safe_h = height_dots - forbid_bottom_px
    block_h = max(0, safe_h - text_top - 8)
    block_w = width_dots - 2*LEFT_MARGIN
    text_blobs = []
    for key in ("ingredients", "notes"):
        val = str(data.get(key, "")).strip()
        if val:
            text_blobs.append(val)
    block_text = "\n\n".join(text_blobs)
    if block_h > 0 and block_text:
        wrapped = text_wrap(draw, block_text, font=f_text, max_width=block_w)
        lines = wrapped.splitlines()
        line_h = f_text.size + 6
        max_lines = max(1, block_h // line_h)
        if len(lines) > max_lines:
            lines = lines[:max_lines-1] + ["..."]
        draw.multiline_text((LEFT_MARGIN, text_top), "\n".join(lines), font=f_text, fill=(0,0,0), spacing=6)

    if ROTATE_180:
        canvas = canvas.rotate(180, expand=False)
    return canvas

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

def pad_rows_to_device_width(raw: bytes, label_wb: int, device_wb: int, rows: int, align: str = "center") -> bytes:
    assert device_wb >= label_wb
    out = bytearray(device_wb * rows)
    pad_total = device_wb - label_wb
    if align == "left":
        pad_left = 0
    elif align == "right":
        pad_left = pad_total
    else:
        pad_left = pad_total // 2
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
    if img.size != (WIDTH_DOTS, HEIGHT_DOTS):
        img = img.resize((WIDTH_DOTS, HEIGHT_DOTS), Image.LANCZOS)
    raw_label, label_wb, rows = to_1bit_bytes(img, WIDTH_DOTS)
    raw_padded = pad_rows_to_device_width(raw_label, label_wb=label_wb, device_wb=DEVICE_WIDTH_BYTES, rows=rows, align="center")

    # Önizleme dosyaları ve GUI önizleme callback
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
# Terazi / AD2K Fonksiyonları
# =========================
def make_ad2k_frame(command_bytes):
    frame = b'\x02' + command_bytes + b'\x03'
    bcc = 0
    for b in frame:
        bcc ^= b
    return frame + bytes([bcc])

def send_ad2k_command(ser, command_bytes, response_timeout=1.0):
    ser.reset_input_buffer()
    ser.write(b'\x13')  # Xoff
    time.sleep(0.02)
    frame = make_ad2k_frame(command_bytes)
    ser.write(frame)
    time.sleep(0.02)
    ser.write(b'\x11')  # Xon
    ser.flush()
    time.sleep(response_timeout)
    resp = b""
    start = time.time()
    while time.time() - start < response_timeout:
        part = ser.read(ser.in_waiting or 1)
        if part:
            resp += part
        else:
            time.sleep(0.01)
    return resp

def send_terazi_handshake_ad2k_commands(ser):
    commands = [
        bytes([0x13]),
        bytes([0x02, 0x57, 0x54, 0x03, 0x23]),
        bytes([0x11]),
        bytes([0x13]),
        bytes([0x11]),
        bytes([0x13]),
        bytes([0x02, 0x57, 0x64, 0x30, 0x30, 0x30, 0x37, 0x37, 0x31, 0x34, 0x38, 0x03, 0x3E]),
        bytes([0x11]),
        bytes([0x13]),
        bytes([0x02, 0x52, 0x43, 0x03, 0x31]),
    ]
    for idx, cmd in enumerate(commands, 1):
        ser.write(cmd)
        ser.flush()
        time.sleep(0.1)

def parse_weight_line(line):
    if isinstance(line, bytes):
        line = line.decode(errors="ignore")
    match = re.search(r'\b0000(\d),(\d{3})', line)
    if match:
        kg = int(match.group(1))
        gr = int(match.group(2))
        total_gram = kg * 1000 + gr
        if total_gram < 5:
            return None
        return total_gram
    return None

def stable_value(stable_queue: deque, tolerance: int) -> bool:
    if len(stable_queue) < stable_queue.maxlen:
        return False
    return (max(stable_queue) - min(stable_queue)) <= tolerance

# =========================
# Port Keşfi Yardımcıları
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
    ports = list(list_ports.comports())
    if not ports:
        return None
    tokens_primary = ["ftdi", "ad", "terazi", "scale", "weigh"]
    tokens_secondary = ["usb", "serial", "com"]
    for p in ports:
        if _port_matches(tokens_primary, p):
            return p.device
    for p in ports:
        if _port_matches(tokens_secondary, p):
            return p.device
    return ports[0].device

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
        self.geometry("980x720")
        self.minsize(900, 640)

        # Paylaşılan durum
        self.stop_event = threading.Event()
        self.log_q: queue.Queue[str] = queue.Queue()

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

        # Tartı kararlılık ve ağırlık durumu
        self.stable_queue: deque[int] = deque(maxlen=STABLE_COUNT)
        self.last_printed_weight: Optional[int] = None
        self.sent_last_weight: Optional[int] = None
        self.weight_var = tk.StringVar(value="0 g")
        self.weight_kg_var = tk.StringVar(value="0.000 kg")
        self.stable_var = tk.StringVar(value="Kararsız")
        self.job_status_var = tk.StringVar(value="Bekleniyor...")
        self.scale_port_var = tk.StringVar(value="(yok)")
        self.printer_port_var = tk.StringVar(value="(yok)")

        # Önizleme resmi
        self.preview_canvas = None
        self.preview_photo = None

        # UI kur
        self._build_ui()

        # Otomatik port keşfi ve bağlan
        self._auto_connect()

        # İş parçacıkları
        self.job_thread = threading.Thread(target=self._job_worker, name="JobWorker", daemon=True)
        self.scale_thread = threading.Thread(target=self._scale_worker, name="ScaleWorker", daemon=True)
        self.job_thread.start()
        self.scale_thread.start()

        # GUI log/panel güncelleme döngüsü
        self.after(100, self._gui_pulse)

        # Kapatma yakalama
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI ----------
    def _build_ui(self):
        pad = 8

        top_frame = ttk.Frame(self)
        top_frame.pack(fill="x", padx=pad, pady=pad)

        # Port durumu ve kontrol
        ttk.Label(top_frame, text="Terazi Port:").grid(row=0, column=0, sticky="w")
        ttk.Label(top_frame, textvariable=self.scale_port_var).grid(row=0, column=1, sticky="w", padx=(4,16))

        ttk.Label(top_frame, text="Yazıcı Port:").grid(row=0, column=2, sticky="w")
        ttk.Label(top_frame, textvariable=self.printer_port_var).grid(row=0, column=3, sticky="w", padx=(4,16))

        ttk.Button(top_frame, text="Portları Yenile", command=self._refresh_ports).grid(row=0, column=4, padx=4)
        ttk.Button(top_frame, text="Yeniden Bağlan", command=self._reconnect_ports).grid(row=0, column=5, padx=4)

        # Orta bölüm: Ağırlık ve kontrol butonları
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

        ctrl_frame = ttk.LabelFrame(mid, text="Kontroller")
        ctrl_frame.pack(side="left", fill="y", padx=(0, pad), pady=(0, pad))

        ttk.Button(ctrl_frame, text="Dara (Tare)", command=self._do_tare, width=16).pack(padx=pad, pady=4)
        ttk.Button(ctrl_frame, text="Sıfır (Zero)", command=self._do_zero, width=16).pack(padx=pad, pady=4)

        ttk.Separator(ctrl_frame, orient="horizontal").pack(fill="x", padx=pad, pady=6)

        ttk.Button(ctrl_frame, text="Start (Yerel)", command=self._local_start, width=16).pack(padx=pad, pady=4)
        ttk.Button(ctrl_frame, text="Done (Yerel)", command=self._local_done, width=16).pack(padx=pad, pady=4)

        ttk.Checkbutton(ctrl_frame, text="Preview Only", variable=self.preview_only).pack(padx=pad, pady=6)

        # Sağ: Etiket önizleme
        right = ttk.LabelFrame(self, text="Etiket Önizleme (son basılan)")
        right.pack(fill="both", expand=True, padx=pad, pady=(0, pad))

        self.preview_canvas = tk.Canvas(right, width=360, height=360, bg="#f2f2f2", highlightthickness=1, relief="sunken")
        self.preview_canvas.pack(side="left", padx=pad, pady=pad)

        # Log alanı
        log_frame = ttk.LabelFrame(right, text="Kayıt Günlüğü")
        log_frame.pack(side="left", fill="both", expand=True, padx=(0, pad), pady=pad)

        self.log_text = tk.Text(log_frame, height=14, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)
        ttk.Button(log_frame, text="Günlüğü Temizle", command=self._clear_log).pack(anchor="e", padx=pad, pady=(4,0))

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

    def _reconnect_ports(self):
        # Terazi
        scl = self.scale_port_var.get()
        if scl and scl != "(yok)":
            try:
                if self.ser_terazi and self.ser_terazi.is_open:
                    self.ser_terazi.close()
                self.ser_terazi = serial.Serial(
                    port=scl, baudrate=SCL_BAUD, bytesize=serial.EIGHTBITS,
                    parity=SCL_PARITY, stopbits=serial.STOPBITS_ONE, timeout=SCL_TIMEOUT,
                )
                time.sleep(0.15)
                send_terazi_handshake_ad2k_commands(self.ser_terazi)
                self._log(f"Terazi bağlandı: {scl}")
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

    # ---------- İş parçacıkları ----------
    def _job_worker(self):
        """
        Odoo'dan komut çeker ve "print_series/print_fixed" gibi terazisiz baskıları yürütür.
        'start'/'done' durumlarını günceller. ScaleWorker baskı tetikler (stabil ağırlıkta).
        """
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
                        if not payload.get("font_path"):
                            payload["font_path"] = DEFAULT_FONT_PATH

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
            # Döngü gecikmesi
            for _ in range(5):
                if self.stop_event.is_set():
                    break
                time.sleep(0.05)

    def _scale_worker(self):
        """
        Tartıdan periyodik okuma yapar, GUI'yi günceller.
        Eğer akış etkin (Odoo veya yerel) ve mrp_id mevcutsa, stabil ve farklı ağırlıkta baskıyı tetikler.
        """
        buffer = b""
        while not self.stop_event.is_set():
            try:
                # Terazi bağlıysa oku
                if not (self.ser_terazi and self.ser_terazi.is_open):
                    time.sleep(0.2)
                    continue

                resp = send_ad2k_command(self.ser_terazi, b'RN\x1C', response_timeout=0.4)
                buffer += resp
                while b"\r" in buffer:
                    line, buffer = buffer.split(b"\r", 1)
                    weight = parse_weight_line(line)
                    if weight is None:
                        continue
                    # Ağırlık gösterimi
                    self._update_weight_display(weight)

                    # Kararlılık kuyruğu
                    self.stable_queue.append(weight)
                    is_stable = stable_value(self.stable_queue, SENSITIVITY_GRAM)
                    self._set_stable(is_stable)

                    # Baskı koşulları
                    if not self._effective_sending():
                        continue
                    mrp_id = self.current_mrp_id
                    if not mrp_id:
                        continue
                    if not is_stable:
                        continue
                    if self.sent_last_weight is not None and abs(self.sent_last_weight - weight) < SENSITIVITY_GRAM:
                        continue

                    # Odoo'dan payload ve baskı
                    payload_from_odoo, resp_copies = self._fetch_label_payload_from_odoo(mrp_id, weight)
                    if payload_from_odoo is None:
                        self._log("Odoo payload alınamadı; baskı atlandı.")
                        self.stable_queue.clear()
                        self.sent_last_weight = weight
                        continue

                    payload = dict(payload_from_odoo)
                    if not payload.get("font_path"):
                        payload["font_path"] = DEFAULT_FONT_PATH

                    copies_to_print = 1 if self.print_single_mode else self._compute_copies({}, resp_copies, payload)
                    copies_to_print = max(1, copies_to_print)

                    for i in range(copies_to_print):
                        self._send_label(payload)
                        self._log(f"Baskı OK ({i+1}/{copies_to_print}) – {weight} g")
                        self.last_printed_weight = weight

                    self.stable_queue.clear()
                    self.sent_last_weight = weight

                    if self.print_single_mode:
                        # Tek baskı modu: akışı kapat
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
        # Etiket önizleme alanına sığdır
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

    def _gui_pulse(self):
        # Log'ları boşalt
        drained = False
        while True:
            try:
                line = self.log_q.get_nowait()
            except queue.Empty:
                break
            else:
                drained = True
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        # Periyodik tekrar
        self.after(100, self._gui_pulse)

    def _on_close(self):
        if messagebox.askokcancel("Çıkış", "Uygulamadan çıkılsın mı?"):
            self.stop_event.set()
            # Bağlantıları kapat
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

    # ---------- Ağ ve iş mantığı yardımcıları ----------
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
                self._log(f"Label fetch HTTP: {r.status_code} {r.text[:100]}")
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