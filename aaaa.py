#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Metin + barkod içeren etiket bitmap'i üretir, 180° döndürür, 1-bit (packed) hale getirip
TSPL (TSC / Argox benzeri) BITMAP komutu olarak seri porta gönderir veya dosyaya yazar.

Görsel / kamera yok. Sadece yazı ve barkod.

Gereken paketler:
    pip install pillow python-barcode pyserial numpy

Örnek kullanım (sadece dosyaya PNG & TSPL çıktısı):
    python aaaa.py --barcode 8684617390017 --date 29.11.2025 --out-png etiket.png --out-tspl etiket.tspl

Seri porta göndermek (örneğin /dev/ttyUSB0):
    python aaaa.py --port /dev/ttyUSB0 --baud 115200 --barcode 8684617390017 --date 29.11.2025 --print

Windows:
    python aaaa.py --port COM5 --barcode 8684617390017 --date 29.11.2025 --print

Etiket genişliği (piksel) yazıcının DPİ ve fiziksel genişliğine göre ayarlayın.
Örn: 203 DPI 60 mm ~ 480 px civarı. Varsayılan 576 (72 mm @203DPI approx).
Genişlik mutlaka 8'in katına yuvarlanır.

Notlar:
- Türkçe karakter desteği için DejaVuSans.ttf (veya uygun bir TTF) gereklidir.
  Sistemde yoksa --font ile path verin.
- Barkod EAN13 ise 12 haneli girersen 13. haneyi otomatik hesaplar. 13 girersen olduğu gibi kullanır.

Üretilen komut sırası (varsayılan):
    SIZE <mmX>,<mmY>
    GAP 2 mm,0
    CLS
    BITMAP ...
    PRINT 1

İstersen --no-header ile sadece BITMAP komutu üret.
"""

import argparse
import sys
import time
from pathlib import Path
import io
import serial
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from barcode import EAN13
from barcode.writer import ImageWriter


# ---------- İçerik Şablonu (İstersen parametrelerle override) ----------

DEFAULT_INGREDIENTS = (
    "EMÜLSİFİYE ET ÜRÜNÜ\n"
    "İÇİNDEKİLER: PİLİÇ ETİ VE YAĞI, MEKANİK AYRILMIŞ PİLİÇ ETİ,\n"
    "SU, PATATES NİŞASTASI, BAHARAT KARIŞIMLARI, TUZ,\n"
    "BİTKİSEL LİF, TUTUCU, AROMA VERİCİ, STABİLİZÖR(E452),\n"
    "ANTOKSİD/(E300), KORUYUCU(E262), RENKLENDİRİCİ (HIBISCUS)\n"
    "ALERJEN UYARISI: BAHARAT KAYNAKLI BİTKİSEL PROTEİN İÇEREBİLİR."
)

DEFAULT_BOTTOM = (
    "Saklama koşulları: Buzdolabında 0/+4°C'de muhafaza ediniz.\n"
    "Parti-seri no: Son tüketim tarihidir.\n"
    "Ürünlerimiz islami usullere göre veteriner hekim kontrolünde kesilmiş etlerden üretilmektedir.\n"
    "Ürünlerimizde domuz ve türevleri yoktur."
)

# ---------- Yardımcı Fonksiyonlar ----------

def prepare_font(path: str | None, size: int):
    if path:
        return ImageFont.truetype(path, size)
    # Sistem font fallback
    # DejaVuSans yoksa Pillow default (load_default) Türkçe karakterleri kare gösterebilir
    for guess in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/DejaVuSans.ttf",
        "C:/Windows/Fonts/DejaVuSans.ttf",
    ]:
        if Path(guess).exists():
            return ImageFont.truetype(guess, size)
    return ImageFont.load_default()


def generate_ean13(barcode_data: str) -> Image.Image:
    """
    python-barcode EAN13 için 12 hane bekler (13. checksum hesaplar).
    Kullanıcı 13 hane girdiyse (ve geçerliyse) onu da kullanabiliriz;
    fakat library 13 verirsek ValueError atabilir. Bu yüzden 13 hane ise
    ilk 12'yi alıp hesaplatıyoruz, checksum farkı oluşursa uyarı verebiliriz.
    """
    raw = barcode_data.strip()
    if not raw.isdigit():
        raise ValueError("Barkod sadece rakamlardan oluşmalıdır.")
    if len(raw) == 13:
        # library 12 hane ister -> ilk 12'yi verelim
        core = raw[:12]
    elif len(raw) == 12:
        core = raw
    else:
        raise ValueError("EAN13 için 12 veya 13 hane giriniz.")
    ean = EAN13(core, writer=ImageWriter())
    # Hafıza üstüne yaz
    buf = io.BytesIO()
    ean.write(buf, {
        "module_height": 40,
        "module_width": 2,  # satır genişliği
        "font_size": 16,
        "quiet_zone": 4,
        "text_distance": 2,
        "write_text": True
    })
    buf.seek(0)
    img = Image.open(buf).convert("L")
    # Barkod görüntüsünü threshold ile siyah beyaz (0/255) hale getir
    return img.point(lambda p: 0 if p < 128 else 255, mode='1').convert("1")


def draw_label_canvas(
    width: int,
    height: int,
    product_name: str,
    weight_str: str,
    date_str: str,
    barcode_data: str,
    ingredients: str,
    bottom_text: str,
    font_path: str | None,
    rotate_180: bool = True
) -> Image.Image:
    """
    Ana etiket alanını çizer. Monokrom (1-bit) olmayan önce 'L'/RGB çizilir sonra threshold.
    180° döndürme en sonda uygulanır.
    """
    # Başlangıçta beyaz canvas (L)
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)

    # Fontlar
    font_title = prepare_font(font_path, 34)
    font_sub = prepare_font(font_path, 26)
    font_norm = prepare_font(font_path, 20)
    font_small = prepare_font(font_path, 18)

    padding = 20
    cursor_y = padding

    # Ürün Adı (Merkeze yakın)
    draw.text((padding, cursor_y), product_name, font=font_title, fill=0)
    cursor_y += font_title.size + 10

    # Ağırlık ve S.T.T satırı
    line1 = f"Ağırlık: {weight_str}"
    line2 = f"S.T.T: {date_str}"
    draw.text((padding, cursor_y), line1, font=font_sub, fill=0)
    # Tarihi biraz sağa yerleştirelim
    draw.text((width // 2, cursor_y), line2, font=font_sub, fill=0)
    cursor_y += font_sub.size + 18

    # Barkod
    barcode_img = generate_ean13(barcode_data)
    # Barkodu makul genişliğe sığdır (en fazla width - 2*padding)
    bw = barcode_img.width
    max_barcode_w = width - 2 * padding
    scale = min(max_barcode_w / bw, 1.0)
    if scale < 1.0:
        new_w = int(barcode_img.width * scale)
        new_h = int(barcode_img.height * scale)
        barcode_img = barcode_img.resize((new_w, new_h), Image.NEAREST)

    # Barkodu yerleştir
    img.paste(barcode_img, (padding, cursor_y))
    cursor_y += barcode_img.height + 20

    # İçindekiler / Malzeme
    for line in ingredients.splitlines():
        draw.text((padding, cursor_y), line, font=font_norm, fill=0)
        cursor_y += font_norm.size + 4
        if cursor_y > height * 0.65:  # alttaki bilgilere yer kalsın
            break

    # Alt metin bölgesi
    bottom_start = int(height * 0.70)
    draw.line((padding, bottom_start - 8, width - padding, bottom_start - 8), fill=0, width=2)
    cy = bottom_start
    for line in bottom_text.splitlines():
        if cy + font_small.size + 2 > height - padding:
            break
        draw.text((padding, cy), line, font=font_small, fill=0)
        cy += font_small.size + 2

    # 1-bit (threshold)
    bw_img = img.point(lambda p: 0 if p < 200 else 255, mode='1').convert("1")

    if rotate_180:
        bw_img = bw_img.rotate(180)  # Pillow rotate angle=180 center

    return bw_img


def pack_1bit(image: Image.Image, invert: bool = False):
    """
    Pillow '1' mode (her piksel 0/255) -> 1 bit per pixel packed (MSB soldan).
    Siyah (0) -> bit=1 (cihaz beklentisine uyumlu), beyaz (255)->0 varsayımı.
    invert=True ile terslenebilir.
    """
    if image.mode != "1":
        raise ValueError("pack_1bit: image.mode '1' olmalı")
    w, h = image.size
    row_bytes = (w + 7) // 8
    pixels = np.array(image, dtype=np.uint8)  # 0 veya 255
    out = bytearray(row_bytes * h)
    for y in range(h):
        byte_val = 0
        bit_cnt = 0
        idx = y * row_bytes
        for x in range(w):
            p = pixels[y, x]
            # 0 = siyah, 255 = beyaz
            bit = 1 if p == 0 else 0
            if invert:
                bit ^= 1
            byte_val = (byte_val << 1) | bit
            bit_cnt += 1
            if bit_cnt == 8:
                out[idx] = byte_val
                idx += 1
                byte_val = 0
                bit_cnt = 0
        if bit_cnt != 0:
            byte_val <<= (8 - bit_cnt)
            out[idx] = byte_val
    return bytes(out), row_bytes, h


def build_tspl_bitmap_command(x, y, data_bytes, row_bytes, height, mode=1, line_break=b"\r\n", hex_output=False):
    header = f"BITMAP {x},{y},{row_bytes},{height},{mode},".encode("ascii")
    if hex_output:
        body = ''.join(f"{b:02X}" for b in data_bytes).encode("ascii")
    else:
        body = data_bytes
    return header + body + line_break


def open_serial_if_needed(port: str | None, baud: int):
    if not port:
        return None
    return serial.Serial(
        port=port,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=1
    )


def send(ser, payload: bytes, flush=True):
    if ser is None:
        return
    ser.write(payload)
    if flush:
        ser.flush()


# ---------- Ana Akış ----------

def main():
    p = argparse.ArgumentParser(description="Metin + Barkod etiket bitmap üretici (180° döndürülmüş, 1-bit, TSPL BITMAP).")
    p.add_argument("--port", default="/dev/ttyACM1", help="Seri port (örn /dev/ttyUSB0 veya COM5)")
    p.add_argument("--baud", type=int, default=19200)
    p.add_argument("--product", default="PİLİÇ SOSİS BÜFE 1000 G")
    p.add_argument("--weight", default="1,000 KG")
    p.add_argument("--date", required=True, help="Son tüketim tarihi (örn 29.11.2025)")
    p.add_argument("--barcode", required=True, help="EAN13 barkod (12 veya 13 hane)")
    p.add_argument("--ingredients", default=DEFAULT_INGREDIENTS)
    p.add_argument("--bottom", default=DEFAULT_BOTTOM)
    p.add_argument("--width", type=int, default=576, help="Etiket genişliği (px, 8'in katına yuvarlanır)")
    p.add_argument("--height", type=int, default=400, help="Etiket yüksekliği (px)")
    p.add_argument("--font", help="TTF font yolu (Türkçe karakterler için)")
    p.add_argument("--invert", action="store_true", help="Siyah/beyaz bitlerini tersle (nadir gerek)")
    p.add_argument("--hex", action="store_true", help="BITMAP verisini HEX ASCII gönder")
    p.add_argument("--no-header", action="store_true", help="SIZE/GAP/CLS/PRINT komutlarını Ekleme")
    p.add_argument("--x", type=int, default=0, help="BITMAP x konumu (dot)")
    p.add_argument("--y", type=int, default=0, help="BITMAP y konumu (dot)")
    p.add_argument("--mode", type=int, default=1, help="TSPL BITMAP mode parametresi (0/1)")
    p.add_argument("--out-png", help="Üretilen 1-bit etiket PNG kaydı (debug/önizleme)")
    p.add_argument("--out-tspl", help="TSPL komutlarını dosyaya yaz")
    p.add_argument("--print", action="store_true", help="Seri porta gerçekten gönder")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    # Genişliği 8'in katına yuvarla
    w = (args.width + 7) // 8 * 8
    h = args.height

    if not args.quiet:
        print(f"[INFO] Boyut (yuvarlanmış) = {w}x{h}")

    # Etiket görüntüsünü oluştur
    label_img = draw_label_canvas(
        width=w,
        height=h,
        product_name=args.product,
        weight_str=args.weight,
        date_str=args.date,
        barcode_data=args.barcode,
        ingredients=args.ingredients,
        bottom_text=args.bottom,
        font_path=args.font,
        rotate_180=True  # 180° döndürme isteği
    )

    if args.out_png:
        label_img.save(args.out_png)
        if not args.quiet:
            print(f"[INFO] PNG kaydedildi: {args.out_png}")

    # 1-bit paket
    packed, row_bytes, height = pack_1bit(label_img, invert=args.invert)

    # TSPL BITMAP komutu
    bitmap_cmd = build_tspl_bitmap_command(
        x=args.x,
        y=args.y,
        data_bytes=packed,
        row_bytes=row_bytes,
        height=height,
        mode=args.mode,
        hex_output=args.hex
    )

    parts = []
    if not args.no_header:
        # Burada etiket fiziksel boyutlarını mm cinsinden tahmin etmek istersen DPI varsayımı (203) ile çevirebilirsin.
        # Basitçe sabit verelim veya istersen parametreleştir: --mmw, --mmh
        mmw = round(w / 8)  # çok kaba yaklaşım (203 DPI varsayımı yok), gerekirse düzelt
        mmh = round(h / 8)
        parts.append(f"SIZE {mmw} mm,{mmh} mm\r\n".encode("ascii"))
        parts.append(b"GAP 2 mm,0\r\n")
        parts.append(b"CLS\r\n")
    parts.append(bitmap_cmd)
    if not args.no_header:
        parts.append(b"PRINT 1\r\n")

    full_payload = b"".join(parts)

    # Dosyaya yaz
    if args.out_tspl:
        Path(args.out_tspl).write_bytes(full_payload)
        if not args.quiet:
            print(f"[INFO] TSPL komut dosyası: {args.out_tspl}")

    # Seri porta gönder
    if args.print:
        ser = open_serial_if_needed(args.port, args.baud)
        if ser is None:
            print("[HATA] --print için --port belirtilmeli", file=sys.stderr)
            sys.exit(2)
        send(ser, full_payload)
        ser.close()
        if not args.quiet:
            print("[INFO] Yazıcıya gönderildi.")

    # Ekrana kısa özet
    if not args.quiet:
        print(f"[INFO] BITMAP row_bytes={row_bytes} height={height} toplam veri={len(packed)} byte")
        print("[INFO] İşlem tamam.")


if __name__ == "__main__":
    main()