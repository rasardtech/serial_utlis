#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minimal / Temiz GS v0 (1D 76 30) Raster Etiket Yazdırma Scripti
Cihaz: Topway / ESC/POS uyumlu (senin model – ESC 0x56 işe yaramadı, GS v 0 çalışacak)

ÖZELLİKLER
-----------
- Önce net bir HANDSHAKE (senin verdiğin sekans + temel ayarlar)
- GS v 0 ile tek seferde tam bitmap gönderimi
- 50x50 mm içerik + 20 mm alt boşluk (toplam 70 mm) varsayılan
- 180° rotate (varsayılan açık, kapatmak için --no-rotate)
- CODE128 barkod (python-barcode varsa), yoksa pseudo fallback
- DOT per mm 8 veya 12 seçilebilir (varsayılan 8; siyah blok genişliğini ölçerek doğrula)
- İsteğe bağlı siyah test bloğu (--black-test) (handshake sonrası)
- Parametresiz çalışır (default test verisi)

KOMUT ÖRNEKLERİ
---------------
1) Sadece siyah blok (genişlik testi):
   python3 gs_label_printer.py --black-test --dot-per-mm 8 --debug

2) Etiketi bas (varsayılan 50x50 + 20 mm boşluk):
   python3 gs_label_printer.py --dot-per-mm 8 --product "ÜRÜN ADI" --barcode "8684617390154" --debug

3) Tüm alanı 70x70 kullan (alt boşluk yok, içerik kutusunu büyüt):
   python3 gs_label_printer.py --content-width 70 --content-height 70 --bottom-blank 0

4) 12 dot/mm dene (eğer siyah blok fiziksel olarak ~47 mm çıkarsa aslında kafa 70mm*12 = 840 dot’tur):
   python3 gs_label_printer.py --dot-per-mm 12

NOTLAR
------
- Genişlik doğrulama: 70 mm hedef. Siyah blok çıktısı cetvelle ölç:
    * ~70 mm ise seçtiğin dot/mm doğru.
    * ~46-47 mm civarı çıkıyorsa aslında kafa 70 mm * 12 dot/mm -> 840 dot; sen 8 kullanmışsın → 12’ye geç.
- Handshake tek sefer port açılınca yapılır. Aynı program içinde ardışık baskılar için tekrar gerekmez.
- Barkod okutulmazsa threshold düşür (–threshold 170) veya module_width değiştir (kodu içinde ayar).

GEREKSİNİMLER
-------------
pip install pillow pyserial python-barcode
(Barcode kütüphanesi yoksa pseudo bir desen basar – üretim için gerçek kütüphaneyi kur.)

"""

import sys
import time
import argparse
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

try:
    import serial
except ImportError:
    serial = None

# Barkod opsiyonel:
try:
    from barcode import Code128
    from barcode.writer import ImageWriter
    HAVE_PYTHON_BARCODE = True
except ImportError:
    HAVE_PYTHON_BARCODE = False


# ===================== VERİ SINIFLARI =====================

@dataclass
class LabelData:
    product_name: str
    variant_line: str
    weight_text: str
    expiry_date: str
    barcode_value: str
    ingredients_lines: List[str] = field(default_factory=list)
    allergy_note: str = ""


@dataclass
class Config:
    port: str = "/dev/ttyACM0"
    baudrate: int = 19200
    timeout: float = 0.8
    dot_per_mm: int = 8          # 8 veya 12 dene
    head_width_mm: int = 70
    content_box_mm: Tuple[int, int] = (50, 50)
    bottom_blank_mm: int = 20
    rotate_180: bool = True
    threshold: int = 192
    speed: int = 1               # 0..3
    density: int = 100           # 65..135
    paper_type: int = 2          # 1:continuous 2:gap 3:blackmark
    debug: bool = False
    black_test: bool = False
    font_scale: float = 1.0
    left_content_mm: Optional[float] = None
    barcode_module_width: float = 0.25  # python-barcode module_width
    handshake_delay: float = 0.05       # her alt komut sonrası bekleme


# ===================== SERİ ARAYÜZ / HANDSHAKE =====================

class Printer:
    def __init__(self, cfg: Config):
        if serial is None:
            raise RuntimeError("pyserial yok: pip install pyserial")
        self.cfg = cfg
        self.ser = serial.Serial(
            cfg.port,
            baudrate=cfg.baudrate,
            timeout=cfg.timeout
            # RTS/CTS gerekirse: rtscts=True
        )
        if self.cfg.debug:
            print(f"[INFO] Port açıldı: {cfg.port} {cfg.baudrate}bps")

    def close(self):
        try:
            self.ser.close()
        except:
            pass

    def _send(self, data: bytes, desc=""):
        self.ser.write(data)
        self.ser.flush()
        if self.cfg.debug:
            if len(data) <= 64:
                print(f"[TX {desc}] {data.hex(' ')}")
            else:
                print(f"[TX {desc}] {len(data)} bytes (head={data[:32].hex()})")
        time.sleep(self.cfg.handshake_delay)

    def handshake(self):
        # 1) Reset + senkron AA55 (senin cihaz için gerekli)
        self._send(b"\x1B@\x1B@\x1B@\x1B@\x1B@\xAA\x55", "reset+sync")
        # 2) Bit image MSB mode
        self._send(b"\x1B=\x01", "bit-image-msb")
        # 3) Paper type
        pt = self.cfg.paper_type if self.cfg.paper_type in (1, 2, 3) else 2
        self._send(bytes([0x12, 0x2F, pt]), "paper-type")
        # 4) Sensör/label config (firmware özel)
        self._send(b"\x12\x70\x03\x00", "sensor-config")
        # 5) Speed
        self._send(bytes([0x12, 0x3C, min(max(self.cfg.speed, 0), 3)]), "speed")
        # 6) Density
        self._send(bytes([0x12, 0x7E, min(max(self.cfg.density, 65), 135)]), "density")
        if self.cfg.debug:
            print("[INFO] Handshake tamamlandı.")

    # GS v 0: (1D 76 30 m xL xH yL yH [data])
    def send_gs_v0_bitmap(self, img: Image.Image):
        w = img.width
        h = img.height
        w_bytes = (w + 7) // 8

        # 1-bit paket hazırlama
        gray = img.convert("L")
        px = gray.load()
        buf = bytearray(w_bytes * h)
        threshold = self.cfg.threshold
        for y in range(h):
            byte = 0
            bit = 7
            base = y * w_bytes
            out = 0
            for x in range(w):
                if px[x, y] < threshold:
                    byte |= (1 << bit)
                bit -= 1
                if bit < 0:
                    buf[base + out] = byte
                    out += 1
                    byte = 0
                    bit = 7
            if bit != 7:
                buf[base + out] = byte  # kalan
        header = bytes([
            0x1D, 0x76, 0x30, 0x00,
            w_bytes & 0xFF, (w_bytes >> 8) & 0xFF,
            h & 0xFF, (h >> 8) & 0xFF
        ])
        self._send(header + buf, "GSv0-bitmap")

    def feed(self, lines=3):
        self._send(b"\n" * lines, "feed")

    def black_test_block(self, width_dots: int, height: int = 64):
        w_bytes = (width_dots + 7) // 8
        header = bytes([
            0x1D, 0x76, 0x30, 0x00,
            w_bytes & 0xFF, (w_bytes >> 8) & 0xFF,
            height & 0xFF, (height >> 8) & 0xFF
        ])
        data = bytes([0xFF]) * (w_bytes * height)
        self._send(header + data, "black-block")


# ===================== BARKOD =====================

def build_code128(barcode_value: str, width_px: int, height_px: int, module_width: float) -> Image.Image:
    if not barcode_value:
        return Image.new("RGB", (width_px, height_px), "white")
    if HAVE_PYTHON_BARCODE:
        try:
            code = Code128(barcode_value, writer=ImageWriter())
        except TypeError:
            code = Code128(barcode_value, writer=ImageWriter())
        writer_options = {
            "module_width": module_width,
            "module_height": height_px,
            "quiet_zone": 1,
            "font_size": 0,
            "text_distance": 1,
            "background": "white",
            "foreground": "black",
            "write_text": False
        }
        img = code.render(writer_options)
        if img.width != width_px or img.height != height_px:
            img = img.resize((width_px, height_px), Image.NEAREST)
        return img.convert("RGB")
    # Fallback pseudo
    img = Image.new("RGB", (width_px, height_px), "white")
    d = ImageDraw.Draw(img)
    x = 0
    for ch in barcode_value:
        w = (ord(ch) & 0x7) + 2
        if x + w >= width_px:
            break
        d.rectangle([x, 0, x + w - 1, height_px], fill="black")
        x += w + 2
    return img


# ===================== METİN SARMA =====================

def load_font(size_px: int, bold=False) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", size_px)
    except:
        return ImageFont.load_default()

def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    if not text:
        return []
    words = text.replace("\n", " ").split()
    lines = []
    cur = ""
    for w in words:
        trial = w if not cur else cur + " " + w
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


# ===================== ETİKET GÖRSELİ =====================

def build_label_bitmap(data: LabelData, cfg: Config) -> Image.Image:
    dot_per_mm = cfg.dot_per_mm
    content_w_mm, content_h_mm = cfg.content_box_mm
    total_h_mm = content_h_mm + cfg.bottom_blank_mm

    width_dots = cfg.head_width_mm * dot_per_mm
    height_dots = total_h_mm * dot_per_mm
    content_w_dots = content_w_mm * dot_per_mm
    content_h_dots = content_h_mm * dot_per_mm

    img = Image.new("RGB", (width_dots, height_dots), "white")
    draw = ImageDraw.Draw(img)

    if cfg.left_content_mm is not None:
        left = int(cfg.left_content_mm * dot_per_mm)
    else:
        left = max(0, (width_dots - content_w_dots) // 2)
    top = 0
    right = left + content_w_dots
    bottom = top + content_h_dots

    draw.rounded_rectangle([left, top, right - 1, bottom - 1], radius=10, outline="black", width=2)

    # Font ölçüleri
    base = int(3.2 * dot_per_mm * cfg.font_scale)
    f_title = load_font(base + 4, bold=True)
    f_bold = load_font(base, bold=True)
    f_norm = load_font(base - 2, bold=False)

    y = top + int(2 * dot_per_mm)
    pad_x = int(2 * dot_per_mm)
    max_text_width = content_w_dots - 2 * pad_x
    x_text = left + pad_x

    # Ürün adı
    for line in wrap_text(draw, data.product_name, f_title, max_text_width):
        draw.text((x_text, y), line, font=f_title, fill="black")
        y += int(f_title.size * 1.15)

    # Varyant
    for line in wrap_text(draw, data.variant_line, f_bold, max_text_width):
        draw.text((x_text, y), line, font=f_bold, fill="black")
        y += int(f_bold.size * 1.1)

    y += int(dot_per_mm * 0.5)

    # Ağırlık / STT
    lt = f"Ağırlık: {data.weight_text}"
    rt = f"S.T.T.: {data.expiry_date}"
    lw = draw.textlength(lt, font=f_bold)
    rw = draw.textlength(rt, font=f_bold)
    if lw + rw + dot_per_mm < max_text_width:
        draw.text((x_text, y), lt, font=f_bold, fill="black")
        draw.text((x_text + max_text_width - rw, y), rt, font=f_bold, fill="black")
        y += int(f_bold.size * 1.3)
    else:
        draw.text((x_text, y), lt, font=f_bold, fill="black")
        y += int(f_bold.size * 1.2)
        draw.text((x_text, y), rt, font=f_bold, fill="black")
        y += int(f_bold.size * 1.3)

    y += int(dot_per_mm * 0.5)

    # Barkod
    barcode_h_mm = 12
    barcode_img = build_code128(
        data.barcode_value,
        max_text_width,
        barcode_h_mm * dot_per_mm,
        cfg.barcode_module_width
    )
    img.paste(barcode_img, (x_text, y))
    y += barcode_img.height + int(dot_per_mm * 0.8)
    bw = draw.textlength(data.barcode_value, font=f_bold)
    draw.text((x_text + (max_text_width - bw) / 2, y), data.barcode_value, font=f_bold, fill="black")
    y += int(f_bold.size * 1.4)

    # İçindekiler
    ing_title = "İÇİNDEKİLER:"
    for line in wrap_text(draw, ing_title, f_bold, max_text_width):
        draw.text((x_text, y), line, font=f_bold, fill="black")
        y += int(f_bold.size * 1.2)

    for raw in data.ingredients_lines:
        for l in wrap_text(draw, raw, f_norm, max_text_width):
            draw.text((x_text, y), l, font=f_norm, fill="black")
            y += int(f_norm.size * 1.15)

    # Alerji
    if data.allergy_note:
        y += int(dot_per_mm * 0.6)
        for line in wrap_text(draw, data.allergy_note, f_bold, max_text_width):
            draw.text((x_text, y), line, font=f_bold, fill="black")
            y += int(f_bold.size * 1.15)

    if cfg.rotate_180:
        return img.rotate(180, expand=True)
    return img


# ===================== ARGPARSE =====================

def parse_args():
    ap = argparse.ArgumentParser(description="GS v0 Label Printer (temiz versiyon)")
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--baudrate", type=int, default=19200)
    ap.add_argument("--dot-per-mm", type=int, default=8)
    ap.add_argument("--head-width-mm", type=int, default=70)
    ap.add_argument("--content-width", type=int, default=50)
    ap.add_argument("--content-height", type=int, default=50)
    ap.add_argument("--bottom-blank", type=int, default=20)
    ap.add_argument("--no-rotate", action="store_true", help="180° rotate kapat")
    ap.add_argument("--threshold", type=int, default=192)
    ap.add_argument("--speed", type=int, default=1)
    ap.add_argument("--density", type=int, default=100)
    ap.add_argument("--paper-type", type=int, default=2)
    ap.add_argument("--product", default="PİLİÇ DİLİMLİ SUCJK")
    ap.add_argument("--variant", default="BAHARATLI 750 G SALAI")
    ap.add_argument("--weight", default="0,750 KG")
    ap.add_argument("--expiry", default="28.05.2024")
    ap.add_argument("--barcode", default="8684617390154")
    ap.add_argument("--ingredient", action="append", default=[
        "PİLİÇ ETİ (%60), DANA YAĞI, MEKANİK AYRILMIŞ PİLİÇ ETİ,",
        "SU, TUZ, BAHARAT KARIŞIMLARI, STABİLİZÖR (E451),",
        "ANTI OKSİDAN (E300), DEKSTROZ, SARIMSAK."
    ])
    ap.add_argument("--allergy", default="ALERJEN: BAHARAT KAYNAKLI İZ PROTEİN İÇEREBİLİR.")
    ap.add_argument("--left-mm", type=float, default=None, help="İçerik alanını soldan mm cinsinden sabitle (ortalama yerine).")
    ap.add_argument("--font-scale", type=float, default=1.0)
    ap.add_argument("--barcode-module-width", type=float, default=0.25)
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--black-test", action="store_true", help="Etiket yerine sadece siyah test bloğu gönder.")
    ap.add_argument("--handshake-delay", type=float, default=0.05)
    ap.add_argument("--threshold-low", action="store_true", help="Daha koyu baskı için threshold otomatik 170'e indir.")
    return ap.parse_args()


# ===================== MAIN =====================

def main():
    args = parse_args()
    threshold = 170 if args.threshold_low else args.threshold

    cfg = Config(
        port=args.port,
        baudrate=args.baudrate,
        dot_per_mm=args.dot_per_mm,
        head_width_mm=args.head_width_mm,
        content_box_mm=(args.content_width, args.content_height),
        bottom_blank_mm=args.bottom_blank,
        rotate_180=(not args.no_rotate),
        threshold=threshold,
        speed=args.speed,
        density=args.density,
        paper_type=args.paper_type,
        debug=args.debug,
        black_test=args.black_test,
        font_scale=args.font_scale,
        left_content_mm=args.left_mm,
        barcode_module_width=args.barcode_module_width,
        handshake_delay=args.handshake_delay
    )

    data = LabelData(
        product_name=args.product,
        variant_line=args.variant,
        weight_text=args.weight,
        expiry_date=args.expiry,
        barcode_value=args.barcode,
        ingredients_lines=args.ingredient,
        allergy_note=args.allergy
    )

    # Genişlik & Yükseklik pixel
    width_dots = cfg.head_width_mm * cfg.dot_per_mm
    height_dots = (cfg.content_box_mm[1] + cfg.bottom_blank_mm) * cfg.dot_per_mm if not cfg.black_test else 200

    prn = None
    try:
        prn = Printer(cfg)
        prn.handshake()

        if cfg.black_test:
            if cfg.debug:
                print("[INFO] Siyah blok gönderiliyor (genişlik test).")
            prn.black_test_block(width_dots, height=64)
            prn.feed(2)
            return

        img = build_label_bitmap(data, cfg)
        if cfg.debug:
            img.save("label_debug.png")
            print("[INFO] label_debug.png kaydedildi (pix={}x{})".format(img.width, img.height))

        prn.send_gs_v0_bitmap(img)
        prn.feed(3)
        if cfg.debug:
            print("[INFO] Baskı komutları gönderildi.")
    except Exception as e:
        print("HATA:", e, file=sys.stderr)
    finally:
        if prn:
            prn.close()


if __name__ == "__main__":
    main()