#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Topway TSC1602A Label yazıcı için:
- Etiket verisini JSON / dict ile al
- Metin + EAN13 barkod çizer
- 180° döndürür
- 1-bit raster (siyah=1) paketler
- Handshake komutlarını yollar
- ESC/POS raster (GS v 0) veya TSPL BITMAP komutu ile gönderir (seçilebilir)

Bağımlılık: Pillow
    pip install pillow

Kullanım Örnekleri:
    python label_printer.py --product "PİLİÇ SOSİS BÜFE 1000 G" \
        --weight "1,000 KG" --expiry 2025-11-29 --ean 8684617390017 \
        --width-mm 50 --height-mm 50 --out-png test.png --port-auto

    python label_printer.py --print --port /dev/ttyACM0 --ean 8684617390017

Not:
- Eğer ESC/POS raster baskı çalışmazsa, send_tspl_bitmap(...) kısmını açıp
  ESC/POS gönderimini yorum satırına alarak TSPL modunu dene.
"""

import argparse
import glob
import sys
import time
from dataclasses import dataclass
from typing import Tuple, List, Optional

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    raise SystemExit("Pillow gerekli. Kur: pip install pillow")

try:
    import serial
except ImportError:
    serial = None  # Print modunda lazım olacak

# ------------------- Konfig Sabitleri -------------------
DPI = 203  # Doğrula: TSC1602A 203 DPI varsayımı
DEFAULT_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/DejaVuSans.ttf",
    "C:/Windows/Fonts/DejaVuSans.ttf",
]

HANDSHAKE_CMDS = [
    b"\x1b@\x1b@\x1b@\x1b@\x1b@\xaaU",  # ESC @ reset'i birden fazla + 0xAA 0x55 sync?
    b"\x1b=\x01",                      # ESC '=' 0x01 (bazı yazıcılarda "printer online")
    b"\x12\x45\x01",                   # DC2 'E' 0x01 (özel fonksiyon)
    b"\x12\x70\x03\x00",               # DC2 'p' parametreler
]

# ------------------- Yardımcı Fonksiyonlar -------------------

def mm_to_dots(mm: float, dpi: int = DPI) -> int:
    return int(round(mm * dpi / 25.4))

def load_font(size: int, explicit: Optional[str]=None):
    if explicit:
        try:
            return ImageFont.truetype(explicit, size)
        except Exception:
            print(f"[WARN] Font açılamadı: {explicit}, fallback deneniyor")
    for p in DEFAULT_FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()

# ------------------- EAN13 Barkod Çizimi (Bağımlılıksız) -------------------
# EAN13 yapısı: Baş (guard) 101, orta guard 01010, son guard 101
# Sol taraf (6 hane) parity pattern: tablodan. Sağ taraf (6 hane) sabit şablon (R code).
L_CODES = {
    '0': '0001101','1': '0011001','2': '0010011','3': '0111101','4': '0100011',
    '5': '0110001','6': '0101111','7': '0111011','8': '0110111','9': '0001011'
}
G_CODES = {
    '0': '0100111','1': '0110011','2': '0011011','3': '0100001','4': '0011101',
    '5': '0111001','6': '0000101','7': '0010001','8': '0001001','9': '0010111'
}
R_CODES = {
    '0': '1110010','1': '1100110','2': '1101100','3': '1000010','4': '1011100',
    '5': '1001110','6': '1010000','7': '1000100','8': '1001000','9': '1110100'
}
# Parity tablosu (ilk digit -> 6 sol digit için L/G pariteleri)
PARITY_TABLE = {
    '0': "LLLLLL",
    '1': "LLGLGG",
    '2': "LLGGLG",
    '3': "LLGGGL",
    '4': "LGLLGG",
    '5': "LGGLLG",
    '6': "LGGGLL",
    '7': "LGLGLG",
    '8': "LGLGGL",
    '9': "LGGLGL"
}

def ean13_check_digit(code12: str) -> str:
    s = 0
    for i, ch in enumerate(code12):
        d = int(ch)
        if (i + 1) % 2 == 0:  # even position (2,4,..) weight 3
            s += 3 * d
        else:
            s += d
    return str((10 - (s % 10)) % 10)

def build_ean13_full(code: str) -> str:
    code = ''.join(c for c in code if c.isdigit())
    if len(code) == 12:
        return code + ean13_check_digit(code)
    if len(code) == 13:
        return code
    raise ValueError("EAN13 barkod 12 veya 13 hane olmalı.")

def draw_ean13(draw: ImageDraw.ImageDraw, x: int, y: int, code: str,
               module_w: int = 2, bar_h: int = 60, text_font=None) -> Tuple[int,int]:
    """
    Barkodu (sadece barlar ve alt text) çizer.
    Dönüş: (toplam genişlik px, toplam yükseklik px)
    """
    full = build_ean13_full(code)
    first = full[0]
    left_digits = full[1:7]
    right_digits = full[7:13]

    parity = PARITY_TABLE[first]
    pattern = "101"  # start guard
    # left side
    for d, p in zip(left_digits, parity):
        if p == 'L':
            pattern += L_CODES[d]
        else:
            pattern += G_CODES[d]
    pattern += "01010"  # center guard
    for d in right_digits:
        pattern += R_CODES[d]
    pattern += "101"  # end guard

    total_width = len(pattern) * module_w
    # Çiz
    for i, bit in enumerate(pattern):
        if bit == '1':
            draw.rectangle(
                [x + i * module_w, y, x + (i+1)*module_w - 1, y + bar_h],
                fill=0
            )
    # Alt yazı
    txt_y = y + bar_h + 2
    if text_font:
        draw.text((x, txt_y), full, font=text_font, fill=0)
        txt_h = text_font.size + 4
    else:
        draw.text((x, txt_y), full, fill=0)
        txt_h = 12
    return total_width, bar_h + txt_h + 2

# ------------------- Etiket Render -------------------

@dataclass
class LabelData:
    product_name: str
    net_weight: str
    expiry: str
    ean13: str
    ingredients: List[str]
    rotate_180: bool = True

def render_label(ld: LabelData,
                 width_mm: float,
                 height_mm: float,
                 font_path: Optional[str] = None) -> Image.Image:
    w = mm_to_dots(width_mm)
    h = mm_to_dots(height_mm)
    img = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(img)

    font_big = load_font(34, font_path)
    font_mid = load_font(22, font_path)
    font_small = load_font(18, font_path)

    xpad = 10
    y = 8

    # Ürün adı (uzunsa satır kırma minimal)
    draw.text((xpad, y), ld.product_name, font=font_big, fill=0)
    y += font_big.size + 4

    # Ağırlık + Tarih
    draw.text((xpad, y), f"Ağırlık: {ld.net_weight}", font=font_mid, fill=0)
    # Tarihi sağa yakın koy
    expiry_text = f"S.T.T: {ld.expiry}"
    expiry_w = draw.textlength(expiry_text, font=font_mid)
    draw.text((w - expiry_w - xpad, y), expiry_text, font=font_mid, fill=0)
    y += font_mid.size + 8

    # Barkod
    if ld.ean13:
        bw, bh = draw_ean13(draw, xpad, y, ld.ean13, module_w=2, bar_h=60, text_font=font_small)
        y += bh + 6

    # İçerik satırları (yükseklik yeterse)
    for line in ld.ingredients:
        if y + font_small.size + 2 > h - 10:
            break
        draw.text((xpad, y), line, font=font_small, fill=0)
        y += font_small.size + 2

    if ld.rotate_180:
        img = img.rotate(180)

    # 1-bit’e indir (Pillow threshold)
    return img.convert("1")

# ------------------- 1-Bit Paketleme -------------------

def pack_1bit(img: Image.Image, invert=False) -> Tuple[bytes, int, int]:
    if img.mode != "1":
        img = img.convert("1")
    w, h = img.size
    row_bytes = (w + 7) // 8
    px = img.load()
    out = bytearray(row_bytes * h)
    for y in range(h):
        byte_val = 0
        bit_count = 0
        idx = y * row_bytes
        for x in range(w):
            # Pillow '1' -> siyah 0, beyaz 255
            bit = 1 if px[x, y] == 0 else 0
            if invert:
                bit ^= 1
            byte_val = (byte_val << 1) | bit
            bit_count += 1
            if bit_count == 8:
                out[idx] = byte_val
                idx += 1
                byte_val = 0
                bit_count = 0
        if bit_count:
            byte_val <<= (8 - bit_count)
            out[idx] = byte_val
    return bytes(out), row_bytes, h

# ------------------- ESC/POS Raster Gönderimi -------------------
def send_escpos_raster(ser, data: bytes, row_bytes: int, height: int):
    """
    GS v 0 biçimi: 1D 76 30 m xL xH yL yH <data>
    m=0 (normal)
    xL,xH = width in bytes (little endian)
    yL,yH = height in dots
    """
    xL = row_bytes & 0xFF
    xH = (row_bytes >> 8) & 0xFF
    yL = height & 0xFF
    yH = (height >> 8) & 0xFF
    header = bytes([0x1D, 0x76, 0x30, 0x00, xL, xH, yL, yH])
    ser.write(header + data)
    # Kağıt/etiket feed (deneme amaçlı birkaç satır)
    ser.write(b"\n\n")

# ------------------- TSPL BITMAP Alternatifi -------------------
def send_tspl_bitmap(ser, data: bytes, row_bytes: int, height: int):
    """
    TSPL formatı: BITMAP x,y,width_bytes,height,mode,<BINARY>\r\nPRINT 1\r\n
    """
    cmd = f"CLS\r\nBITMAP 0,0,{row_bytes},{height},1,".encode("ascii") + data + b"\r\nPRINT 1\r\n"
    ser.write(cmd)

# ------------------- Handshake -------------------
def perform_handshake(ser):
    for i, c in enumerate(HANDSHAKE_CMDS):
        ser.write(c)
        ser.flush()
        time.sleep(0.05 if i < len(HANDSHAKE_CMDS)-1 else 0.1)

# ------------------- Port Bulma -------------------
def auto_find_port() -> Optional[str]:
    candidates = sorted(glob.glob("/dev/ttyACM*")) + sorted(glob.glob("/dev/ttyUSB*"))
    return candidates[0] if candidates else None

# ------------------- Ana -------------------
def main():
    ap = argparse.ArgumentParser(description="Topway TSC1602A Label - Etiket Bitmap Yazdırıcı (Pi).")
    ap.add_argument("--product", default="PİLİÇ SOSİS BÜFE 1000 G")
    ap.add_argument("--weight", default="1,000 KG")
    ap.add_argument("--expiry", required=True, help="Son tüketim tarihi (YYYY-MM-DD veya DD.MM.YYYY gösterim)")
    ap.add_argument("--ean", required=True, help="EAN13 barkod (12/13 hane)")
    ap.add_argument("--width-mm", type=float, default=50.0, help="Baskı alanı genişliği mm (örn 50)")
    ap.add_argument("--height-mm", type=float, default=50.0, help="Baskı alanı yüksekliği mm")
    ap.add_argument("--font", help="TTF font path")
    ap.add_argument("--invert", action="store_true", help="Baskıda siyah/beyaz tersle")
    ap.add_argument("--no-rotate", action="store_true", help="180° döndürme olmasın")
    ap.add_argument("--out-png", help="Önizleme PNG kaydet")
    ap.add_argument("--print", action="store_true", help="Yazıcıya gönder")
    ap.add_argument("--port", help="Seri port (örn /dev/ttyACM0)")
    ap.add_argument("--port-auto", action="store_true", help="İlk uygun /dev/ttyACM* otomatik bul")
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--tspl", action="store_true", help="ESC/POS yerine TSPL BITMAP komutu dene")
    args = ap.parse_args()

    ingredients = [
        "EMÜLSİFİYE ET ÜRÜNÜ",
        "İÇİNDEKİLER: PİLİÇ ETİ, SU, BAHARAT, TUZ",
        "ALERJEN: BİTKİSEL PROTEİN KAYNAĞI",
    ]

    ld = LabelData(
        product_name=args.product,
        net_weight=args.weight,
        expiry=args.expiry,
        ean13=args.ean,
        ingredients=ingredients,
        rotate_180=not args.no_rotate
    )

    img = render_label(ld, args.width_mm, args.height_mm, font_path=args.font)
    if args.out_png:
        img.save(args.out_png)
        print(f"[INFO] PNG kaydedildi: {args.out_png}")

    packed, row_bytes, height = pack_1bit(img, invert=args.invert)
    print(f"[INFO] Raster hazır: width={img.width} height={height} row_bytes={row_bytes} totalData={len(packed)} bytes")

    if args.print:
        if serial is None:
            print("[ERR] pyserial kurulmamış (pip install pyserial)", file=sys.stderr)
            sys.exit(2)
        port = args.port
        if args.port_auto:
            port = auto_find_port()
            if not port:
                print("[ERR] Otomatik port bulunamadı.", file=sys.stderr)
                sys.exit(3)
            print(f"[INFO] Otomatik seçilen port: {port}")

        if not port:
            print("[ERR] --print için port gerekli (--port veya --port-auto)", file=sys.stderr)
            sys.exit(4)

        with serial.Serial(port, args.baud, timeout=1) as ser:
            print("[INFO] Handshake başlıyor...")
            perform_handshake(ser)
            print("[INFO] Handshake tamam. Baskı gönderiliyor...")
            if args.tspl:
                send_tspl_bitmap(ser, packed, row_bytes, height)
            else:
                send_escpos_raster(ser, packed, row_bytes, height)
            ser.flush()
            print("[INFO] Baskı komutları gönderildi.")

    print("[DONE]")

if __name__ == "__main__":
    main()