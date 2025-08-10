#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cihaz logundan çıkardığımız komut dizisini /dev/ttyACM0 (19200 baud) üzerinde
yeniden gönderen sadeleştirilmiş sürüm.

ÖZET AKIŞ:
 1) (Opsiyonel) Soft purge
 2) Status sorgusu: 12 65 01 12 65 00  -> 2 bayt cevap bekler (örn: 80 ED / 80 EC)
 3) Birkaç kez ESC @ (init)
 4) AA 55 (özel header)
 5) Parametre DC2 komutları (12 2F 02, 12 3C 01, 12 7E 6E, 12 70 03)
 6) LF
 7) ESC @ ve ESC = 01 (online)
 8) Raster başlangıcı: 1B 56 <width_bytes> <height_param>
 9) Raster veri blokları (varsayılan 0x00 dolu)
10) (Opsiyonel) Son status sorgusu

KOMUT (örnek):
    python send_device_sequence.py
veya (farklı desenle)
    python send_device_sequence.py --pattern checker --raster-chunks 60

Notlar:
- width_bytes: logta 0x40 (64) göründüğü için default 64.
- height_param: logta 0x02 olduğu için default 2.
- raster-chunks: logta çok sayıda blok vardı; burada varsayılan 120.
- pattern: zero | full | checker | gradient
"""

import time
import sys
from typing import Optional
import argparse

try:
    import serial
    from serial import Serial
except ImportError:
    print("pyserial yüklü değil. Kur: pip install pyserial", file=sys.stderr)
    sys.exit(1)

ESC  = 0x1B
DC2  = 0x12
LF   = 0x0A

STATUS_QUERY_SEQ   = bytes([DC2, 0x65, 0x01, DC2, 0x65, 0x00])   # 12 65 01 12 65 00
INIT_CMD           = bytes([ESC, 0x40])                         # ESC @
ONLINE_CMD         = bytes([ESC, 0x3D, 0x01])                   # ESC = 01
CUSTOM_HEADER      = bytes([0xAA, 0x55])                        # AA 55
CUSTOM_PARAM_CMDS  = [
    bytes([DC2, 0x2F, 0x02]),
    bytes([DC2, 0x3C, 0x01]),
    bytes([DC2, 0x7E, 0x6E]),
    bytes([DC2, 0x70, 0x03]),
]
TRIGGER_CMD        = bytes([DC2, 0x3E])                         # 12 3E (logta tekil)
RASTER_START_PREF  = bytes([ESC, 0x56])                         # ESC 56 (özel üretici)

def hexdump(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b)

def soft_purge(ser: Serial):
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
    except Exception:
        pass

def read_status(ser: Serial, timeout=0.2) -> Optional[bytes]:
    old = ser.timeout
    ser.timeout = timeout
    data = ser.read(2)
    ser.timeout = old
    return data if len(data) == 2 else None

def build_raster_start(width_bytes: int, height_param: int) -> bytes:
    return RASTER_START_PREF + bytes([(width_bytes & 0xFF), (height_param & 0xFF)])

def gen_chunk(pattern: str, width_bytes: int) -> bytes:
    if pattern == "zero":
        return bytes([0x00] * width_bytes)
    if pattern == "full":
        return bytes([0xFF] * width_bytes)
    if pattern == "checker":
        buf = bytearray(width_bytes)
        for i in range(width_bytes):
            buf[i] = 0xAA if i % 2 == 0 else 0x55
        return bytes(buf)
    if pattern == "gradient":
        return bytes([(i % 256) for i in range(width_bytes)])
    return bytes([0x00] * width_bytes)

def send(ser: Serial, data: bytes, label: str, delay: float = 0.0):
    ser.write(data)
    ser.flush()
    print(f"[TX] {label:<14} ({len(data)}B) {hexdump(data)}")
    if delay > 0:
        time.sleep(delay)

def sequence(args):
    ser = Serial(
        port=args.port,
        baudrate=args.baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.05,
        write_timeout=0.5,
    )
    print(f"[INFO] Açıldı: {ser.port} @ {args.baud} baud")

    soft_purge(ser)

    # 1) Status
    send(ser, STATUS_QUERY_SEQ, "STATUS1", args.delay_cmd)
    st = read_status(ser, args.status_timeout)
    print(f"[RX] STATUS1 RESP: {hexdump(st) if st else 'yok'}")

    # 2) ESC @ tekrarları
    for i in range(args.init_count):
        send(ser, INIT_CMD, f"INIT{i+1}", args.delay_init)

    # 3) AA 55
    if args.header:
        send(ser, CUSTOM_HEADER, "HEADER", args.delay_cmd)

    # 4) Parametre DC2 komutları
    if args.params:
        for c in CUSTOM_PARAM_CMDS:
            send(ser, c, "PARAM", args.delay_cmd)

    # 5) LF
    if args.send_lf:
        send(ser, bytes([LF]), "LF", args.delay_cmd)

    # Opsiyonel trigger
    if args.trigger:
        send(ser, TRIGGER_CMD, "TRIGGER", args.delay_cmd)

    # 6) İkinci status (opsiyonel)
    if args.status2:
        send(ser, STATUS_QUERY_SEQ, "STATUS2", args.delay_cmd)
        st2 = read_status(ser, args.status_timeout)
        print(f"[RX] STATUS2 RESP: {hexdump(st2) if st2 else 'yok'}")

    # 7) ESC @ + ONLINE
    send(ser, INIT_CMD, "INIT2", args.delay_cmd)
    send(ser, ONLINE_CMD, "ONLINE", args.delay_cmd)

    # 8) Raster başlangıcı
    if args.raster:
        raster_start = build_raster_start(args.width_bytes, args.height_param)
        send(ser, raster_start, "RAST_START", args.delay_cmd)

        for i in range(args.raster_chunks):
            chunk = gen_chunk(args.pattern, args.width_bytes)
            send(ser, chunk, f"RAST#{i+1}", args.delay_raster)
            if args.read_every > 0 and ((i + 1) % args.read_every == 0):
                r = ser.read(16)
                if r:
                    print(f"[RX] MID ({len(r)}B) {hexdump(r)}")

    # 9) Son status
    if args.status_end:
        send(ser, STATUS_QUERY_SEQ, "STATUS_END", args.delay_cmd)
        st3 = read_status(ser, args.status_timeout)
        print(f"[RX] STATUS_END RESP: {hexdump(st3) if st3 else 'yok'}")

    ser.close()
    print("[INFO] Port kapatıldı.")

def parse_args():
    p = argparse.ArgumentParser(description="Cihaz loguna benzer komut dizisi gönderici ( /dev/ttyACM0 @19200 ).")
    p.add_argument("--port", default="/dev/ttyACM0", help="Seri port (varsayılan: /dev/ttyACM0)")
    p.add_argument("--baud", type=int, default=19200, help="Baud rate (varsayılan: 19200)")
    p.add_argument("--init-count", type=int, default=5, help="İlk blokta kaç ESC @")
    p.add_argument("--header", action="store_true", default=True, help="AA 55 gönder (default açık)")
    p.add_argument("--params", action="store_true", default=True, help="Parametre DC2 komut bloklarını gönder (default açık)")
    p.add_argument("--send-lf", action="store_true", default=True, help="LF gönder (default açık)")
    p.add_argument("--trigger", action="store_true", default=False, help="DC2 3E tetik komutunu ekle")
    p.add_argument("--status2", action="store_true", default=False, help="İkinci status sorgusu yap")
    p.add_argument("--raster", action="store_true", default=True, help="Raster aktarımı yap (default açık)")
    p.add_argument("--width-bytes", type=int, default=64, help="Raster satır byte genişliği (logta 0x40)")
    p.add_argument("--height-param", type=int, default=2, help="Raster start ikinci parametre (logta 0x02)")
    p.add_argument("--raster-chunks", type=int, default=120, help="Gönderilecek raster chunk sayısı (tümü sıfır veya pattern)")
    p.add_argument("--pattern", choices=["zero", "full", "checker", "gradient"], default="zero", help="Raster chunk pattern")
    p.add_argument("--read-every", type=int, default=0, help=">0 ise her N chunk sonrası 16 byte oku")
    p.add_argument("--status-end", action="store_true", default=True, help="Sonda status sorgusu (default açık)")
    p.add_argument("--delay-cmd", type=float, default=0.01, help="Komut arası gecikme")
    p.add_argument("--delay-init", type=float, default=0.02, help="ESC @ tekrarları arası gecikme")
    p.add_argument("--delay-raster", type=float, default=0.03, help="Raster chunk arası gecikme")
    p.add_argument("--status-timeout", type=float, default=0.2, help="Status read timeout")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
    try:
        sequence(args)
    except KeyboardInterrupt:
        print("\n[INFO] Kullanıcı iptal etti.")
    except serial.SerialException as e:
        print(f"[HATA] Seri port: {e}")
    except Exception as ex:
        print(f"[HATA] Beklenmeyen: {ex}")