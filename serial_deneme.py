import serial
import sys

# GÖNDERMEK İSTEDİĞİN ETİKET KOMUTU (örneğin Odoo'dan aldığın)
LABEL_COMMAND = b"""
SIZE 60 mm,40 mm
GAP 2 mm,0 mm
CLS
TEXT 20,30,"3",0,1,1,"SUCUK KOMBI"
TEXT 20,70,"3",0,1,1,"Miktar: 382 gr"
TEXT 20,110,"3",0,1,1,"Lot: A-20250730-001"
TEXT 20,150,"3",0,1,1,"Tarih: 2025-07-27 18:10:28"
TEXT 20,190,"3",0,1,1,"SKT: 2025-07-27 18:10:28"
PRINT 1,1
"""

# Seri port ayarları (gerekirse portu güncelle!)
SERIAL_PORT = '/dev/ttyACM0'
BAUDRATE = 9600

def print_label_to_topway(label_command, port=SERIAL_PORT, baud=BAUDRATE):
    try:
        with serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=2
        ) as ser:
            print(f"Yazıcıya etiket gönderiliyor ({port}, {baud} baud)...")
            # TSPL komutunu satır satır gönder, her satırdan sonra kısa bekle (bazı yazıcılar için güvenli)
            for line in label_command.splitlines():
                if line.strip():
                    ser.write(line.strip() + b'\r\n')
            print("Etiket yazdırma komutu gönderildi. (Yazıcıdan çıktı aldıysan her şey çalışıyor demektir!)")
    except Exception as e:
        print("Etiket yazdırma hatası:", e)
        sys.exit(1)

if __name__ == "__main__":
    print_label_to_topway(LABEL_COMMAND)
