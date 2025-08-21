from __future__ import annotations

# Her zaman BITMAP (raster) yazdıran birleşik sürüm.
# - Odoo JSON "label" zorunlu (TSPL fallback yok).
# - compose_label + to_1bit_bytes + ESC 'V' height-only (tek header + tek akış) kullanır.
# - print_single: True ise ilk başarılı baskıdan sonra durur.
# - print_series/print_fixed: terazisiz sabit ağırlıkla N kopya, her seferinde bitmap.
# - create_date kontrolü: print_series/print_fixed için aynı job+m rp_id+create_date daha önce işlendi ise tekrar yazdırma.

import os
import re
import glob
import json
import time
import math
import requests
import serial

from typing import Tuple, Dict, Any, List, Optional
from collections import deque
from PIL import Image, ImageDraw, ImageFont

# =========================
# Odoo Uçları ve Kararlılık
# =========================

GET_JOB_URL = "https://altinayet-stage-22335048.dev.odoo.com/terazi/get_scale_job/1"   # scale_id'yi güncelle
ODOO_URL_TEMPLATE = "https://altinayet-stage-22335048.dev.odoo.com/terazi/get/{mrp_id}/{weight}"

STABLE_COUNT = 5  # Teraziden gelen ağırlık verilerinin kararlılığı için gereken ölçüm sayısı
SENSITIVITY_GRAM = 20

# =========================
# Yazıcı (ESC 'V' raster) Ayarları
# =========================

PREVIEW_ONLY = False
PREVIEW_PNG_PATH = "label_preview.png"
PREVIEW_BMP1_PATH = "label_preview_1b.bmp"
PREVIEW_BIN_PATH  = "label_raster_padded.bin"

PRN_PORT_FALLBACK = "/dev/ttyACM0"
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

DEFAULT_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# =========================
# Terazi (AD2K) Ayarları
# =========================

SCL_BAUD = 19200
SCL_PARITY = serial.PARITY_ODD
SCL_TIMEOUT = 0.5

# =========================
# Yardımcılar (Genel)
# =========================

def auto_serial_port_terazi() -> str:
    ports = glob.glob('/dev/serial/by-id/usb*') + glob.glob('/dev/ttyUSB*')
    for port in ports:
        low = port.lower()
        if 'ftdi' in low or 'ad' in low or 'terazi' in low:
            print("Terazi portu:", port)
            return port
    if ports:
        print("Terazi portu:", ports[0])
        return ports[0]
    raise Exception("Terazi cihazı bağlı değil!")

def auto_serial_port_yazici() -> str:
    ports = glob.glob('/dev/ttyACM*') + glob.glob('/dev/serial/by-id/usb*')
    for port in ports:
        low = port.lower()
        if 'topway' in low or 'printer' in low or 'yazici' in low:
            print("Yazıcı portu:", port)
            return port
    for port in ports:
        if 'ttyACM' in port:
            print("Yazıcı portu:", port)
            return port
    print("Yazıcı port fallback:", PRN_PORT_FALLBACK)
    return PRN_PORT_FALLBACK

def fetch_job() -> Dict[str, Any]:
    try:
        resp = requests.get(GET_JOB_URL, timeout=4)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict):
                return data
    except Exception as e:
        print("Odoo iş emri/komut çekme hatası:", e)
    return {"job": "", "mrp_id": None}

def fetch_label_payload_from_odoo(mrp_id: Any, weight_grams: int) -> Tuple[Optional[Dict[str, Any]], int]:
    """
    Beklenen JSON:
    {
      "label": {
        "font_path": "...",
        "product_name": "...",
        "count": "1",
        "weight_str": "0,706 KG",
        "expiry": "29.11.2025",
        "barcode": "2835172007063",
        "ingredients": "...",
        "notes": "..."
      },
      "copies": 1
    }
    """
    try:
        url = ODOO_URL_TEMPLATE.format(mrp_id=mrp_id, weight=weight_grams)
        r = requests.get(url, timeout=6)
        if r.status_code != 200:
            print("Label fetch HTTP:", r.status_code, r.text[:120])
            return None, 1
        data = r.json()  # Yalnızca JSON kabul edilir (bitmap şart)
        if isinstance(data, dict) and "label" in data:
            payload = data.get("label") or {}
            copies = int(data.get("copies") or 1)
            return payload, copies
        # Direkt dict de olabilir
        return data, int(data.get("copies") or 1) if isinstance(data, dict) else 1
    except Exception as e:
        print("Label fetch/parse error (JSON zorunlu):", e)
        return None, 1

def compute_copies(job: Dict[str, Any], resp_copies: int, payload: Dict[str, Any]) -> int:
    # Öncelik: job.copies > response.copies > payload.count (sayısal) > 1
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

def get_job_token(job: Dict[str, Any]) -> str:
    # create_date tabanlı kimlik; aynı create_date tekrar yazdırılmaz
    return f"{job.get('job','')}|{job.get('mrp_id')}|{job.get('create_date','')}"

# =========================
# Görsel/Raster Yardımcıları
# =========================

def round_to_8(n: int) -> int:
    return int(math.ceil(n / 8.0) * 8)

WIDTH_DOTS = round_to_8(REQ_W)   # 752
HEIGHT_DOTS = REQ_H
LABEL_WIDTH_BYTES = WIDTH_DOTS // 8  # 94

def load_font(font_path: str | None, size: int) -> ImageFont.ImageFont:
    if font_path:
        try:
            return ImageFont.truetype(font_path, size=size)
        except Exception:
            pass
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

# EAN-13
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

# Görsel oluşturma – barkod sağda, bilgiler solda
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

# Raster dönüşüm
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

# Yazıcı protokolü
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

def send_label_image_to_printer(ser_yazici: serial.Serial, payload: Dict[str, Any], feed_after_lines: int = FEED_AFTER_LINES):
    # Görsel
    img = compose_label(payload, WIDTH_DOTS, HEIGHT_DOTS, BOTTOM_FORBID)
    if img.size != (WIDTH_DOTS, HEIGHT_DOTS):
        img = img.resize((WIDTH_DOTS, HEIGHT_DOTS), Image.LANCZOS)
    raw_label, label_wb, rows = to_1bit_bytes(img, WIDTH_DOTS)
    raw_padded = pad_rows_to_device_width(raw_label, label_wb=label_wb, device_wb=DEVICE_WIDTH_BYTES, rows=rows, align="center")

    if PREVIEW_ONLY:
        try:
            img.save(PREVIEW_PNG_PATH)
            img.convert("1").save(PREVIEW_BMP1_PATH, format="BMP")
            with open(PREVIEW_BIN_PATH, "wb") as f:
                f.write(raw_padded)
        except Exception as e:
            print("Önizleme kaydetme hatası:", e)
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
        print(f"Terazi handshake [{idx}]: {cmd.hex(' ')}")
        time.sleep(0.1)
    print("Terazi handshake (AD2K) tamamlandı.")

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

def stable_value(stable_queue, value, tolerance):
    if len(stable_queue) < stable_queue.maxlen:
        return False
    return (max(stable_queue) - min(stable_queue)) <= tolerance

# =========================
# Ana Döngü
# =========================

def main():
    # Portlar
    try:
        scl_port = auto_serial_port_terazi()
    except Exception as e:
        print("Uyarı: Terazi bulunamadı, yalnızca print_series/print_fixed çalışır. Hata:", e)
        scl_port = None

    prn_port = auto_serial_port_yazici()

    # Seri hatlar
    ser_terazi = None
    if scl_port:
        ser_terazi = serial.Serial(
            port=scl_port, baudrate=SCL_BAUD, bytesize=serial.EIGHTBITS,
            parity=SCL_PARITY, stopbits=serial.STOPBITS_ONE, timeout=SCL_TIMEOUT,
        )
        send_terazi_handshake_ad2k_commands(ser_terazi)

    ser_yazici = serial.Serial(
        port=prn_port, baudrate=PRN_BAUD, bytesize=serial.EIGHTBITS,
        parity=PRN_PARITY, stopbits=serial.STOPBITS_ONE, timeout=PRN_TIMEOUT,
    )
    printer_handshake(ser_yazici)

    print("Hazır. Komut bekleniyor...")

    buffer = b""
    stable_queue = deque(maxlen=STABLE_COUNT)
    sent_last_weight = None

    sending_data = False
    print_single_mode = False
    last_action_id = None

    # create_date tabanlı tekrar-baskı önleme (seri işler için)
    processed_series_tokens: set[str] = set()
    MAX_TOKEN_CACHE = 200  # bellek sınırı

    while True:
        # 1) Komutu al
        job = fetch_job()
        job_str = (job.get("job") or "").lower()
        mrp_id = job.get("mrp_id")
        action_id = json.dumps(job, sort_keys=True)

        if job_str and action_id != last_action_id:
            if job_str == "start":
                print_single_mode = bool(job.get("print_single", False))
                sending_data = True
                stable_queue.clear()
                sent_last_weight = None
                print(f"START: print_single={print_single_mode}")
                last_action_id = action_id

            elif job_str == "done":
                sending_data = False
                print("DONE: Tartı akışı kapatıldı.")
                last_action_id = action_id

            elif job_str == "tare":
                if ser_terazi:
                    print("DARA...")
                    _ = send_ad2k_command(ser_terazi, b'T')
                last_action_id = action_id

            elif job_str == "zero":
                if ser_terazi:
                    print("SIFIR...")
                    _ = send_ad2k_command(ser_terazi, b'Z')
                last_action_id = action_id

            elif job_str in ("print_series", "print_n", "print_fixed"):
                # create_date bazlı tekrar-baskı engelle
                token = get_job_token(job)
                if token in processed_series_tokens:
                    print(f"Aynı create_date'li seri iş zaten işlendi (token={token}), baskı atlandı.")
                    last_action_id = action_id
                    continue

                # terazisiz sabit seri
                copies = int(job.get("copies") or 1)
                delay_sec = int(job.get("delay_sec") or 5)
                fixed_weight = int(job.get("weight") or 0)
                payload_override = job.get("payload") or {}
                if isinstance(payload_override, str):
                    try:
                        payload_override = json.loads(payload_override)
                    except Exception:
                        payload_override = {}

                payload_from_odoo, resp_copies = fetch_label_payload_from_odoo(mrp_id, fixed_weight)
                if payload_from_odoo is None:
                    print("Odoo payload alınamadı; baskı atlandı.")
                    last_action_id = action_id
                    continue

                payload = {**payload_from_odoo, **payload_override}
                if not payload.get("font_path"):
                    payload["font_path"] = DEFAULT_FONT_PATH

                eff_copies = copies if copies > 0 else compute_copies(job, resp_copies, payload)
                eff_copies = max(1, eff_copies)

                print(f"PRINT_SERIES: mrp_id={mrp_id}, copies={eff_copies}, delay={delay_sec}s, weight={fixed_weight}")
                for i in range(eff_copies):
                    send_label_image_to_printer(ser_yazici, payload, feed_after_lines=FEED_AFTER_LINES)
                    print(f" -> {i+1}/{eff_copies} basıldı")
                    if i < eff_copies - 1:
                        time.sleep(delay_sec)

                # token'ı işlendi olarak işaretle
                processed_series_tokens.add(token)
                if len(processed_series_tokens) > MAX_TOKEN_CACHE:
                    # set'i çok büyütmemek için en eskileri temizle (basit yöntem: sıfırla)
                    processed_series_tokens = set(list(processed_series_tokens)[-MAX_TOKEN_CACHE:])

                last_action_id = action_id

        # 2) Tartı okuma ve baskı (start/done akışı)
        if sending_data and mrp_id and ser_terazi:
            resp = send_ad2k_command(ser_terazi, b'RN\x1C')
            buffer += resp
            while b"\r" in buffer:
                line, buffer = buffer.split(b"\r", 1)
                weight = parse_weight_line(line)
                if weight is None:
                    continue
                stable_queue.append(weight)
                if not stable_value(stable_queue, weight, SENSITIVITY_GRAM):
                    continue
                if sent_last_weight is not None and abs(sent_last_weight - weight) < SENSITIVITY_GRAM:
                    continue

                payload_from_odoo, resp_copies = fetch_label_payload_from_odoo(mrp_id, weight)
                if payload_from_odoo is None:
                    print("Odoo payload alınamadı; baskı atlandı.")
                    stable_queue.clear()
                    sent_last_weight = weight
                    continue

                payload = dict(payload_from_odoo)
                if not payload.get("font_path"):
                    payload["font_path"] = DEFAULT_FONT_PATH

                copies_to_print = 1 if print_single_mode else compute_copies(job={}, resp_copies=resp_copies, payload=payload)
                copies_to_print = max(1, copies_to_print)

                for i in range(copies_to_print):
                    send_label_image_to_printer(ser_yazici, payload, feed_after_lines=FEED_AFTER_LINES)
                    print(f"Baskı OK ({i+1}/{copies_to_print}) – {weight} gr")

                stable_queue.clear()
                sent_last_weight = weight

                if print_single_mode:
                    sending_data = False  # tek seferde dur
        else:
            time.sleep(0.25)

if __name__ == "__main__":
    main()