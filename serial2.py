import glob
import serial
import requests
import time
import re
from collections import deque

ODOO_URL_TEMPLATE = "https://altinayet-stage-22335048.dev.odoo.com/terazi/get/{mrp_id}/{weight}"
GET_JOB_URL = "https://altinayet-stage-22335048.dev.odoo.com/terazi/get_scale_job/1"   # scale_id'yi uygun gir!

STABLE_COUNT = 20
SENSITIVITY_GRAM = 4

def auto_serial_port():
    ports = glob.glob('/dev/serial/by-id/usb*') or glob.glob('/dev/ttyUSB*')
    if not ports:
        raise Exception("Hiçbir tartı cihazı bağlı değil!")
    print("Kullanılan port:", ports[0])
    return ports[0]

def get_current_job():
    try:
        resp = requests.get(GET_JOB_URL)
        if resp.status_code == 200:
            jobs = resp.json()
            if jobs:
                job = jobs[0]
                return job.get("job", "").lower(), job.get("mrp_id", None)
    except Exception as e:
        print("Odoo iş emri/komut çekme hatası:", e)
    return "", None

def parse_weight_line(line):
    line = line.replace(b'\t', b'').replace(b'\x02', b'').replace(b'\x03', b'').replace(b'\n', b'').strip()
    matches = re.findall(br'(\d{1,8},\d{1,6})', line)
    for match in matches:
        left, right = match.split(b',')
        weight_val = right.decode().lstrip('0') or '0'
        return int(weight_val)
    return None

def send_scale_command(ser, cmd_byte, timeout=1.0):
    ser.write(cmd_byte)
    #ser.flush()
    return False

def main():
    port = auto_serial_port()
    ser = serial.Serial(
        port=port,
        baudrate=19200,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_EVEN,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.5,
    )
    print("Hazır, 'Start' komutunu bekliyor...")

    buffer = b""
    sent_last_weight = None
    stable_queue = deque(maxlen=STABLE_COUNT)
    last_action_id = None  # Son uygulanan komut ("tare:mrp_id" gibi)
    sending_data = False   # Yalnızca 'start' komutundan sonra True olur

    while True:
        job_str, mrp_id = get_current_job()
        action_id = f"{job_str}:{mrp_id}"

        # Komut kontrolü
        if job_str and action_id != last_action_id:
            if "start" in job_str:
                print("START KOMUTU: Tartıdan veri akışı başlatılıyor...")
                send_scale_command(ser, b'\x02\x52\x4E\x1C\x03')  # "N"
                sending_data = True
                last_action_id = action_id
            elif "done" in job_str:
                print("DONE KOMUTU: Tartıdan veri akışı durduruluyor...")
                sending_data = False
                last_action_id = action_id
            elif "tare" in job_str:
                print("DARA KOMUTU gönderiliyor...")
                if send_scale_command(ser, b'\x02T\x03'):
                    last_action_id = action_id
            elif "zero" in job_str:
                print("SIFIRLA KOMUTU gönderiliyor...")
                if send_scale_command(ser, b'\x02Z\x03'):
                    last_action_id = action_id

        # Veri akışı aktifse, veri gönder
        if sending_data and mrp_id:
            ser.write(b'\x02\x52\x4E\x1C\x03')
            chunk = ser.read(128)
            if chunk:
                buffer += chunk
                while b"\r" in buffer:
                    line, buffer = buffer.split(b"\r", 1)
                    weight = parse_weight_line(line)
                    if weight is not None and weight > 60:
                        stable_queue.append(weight)
                        if len(stable_queue) == STABLE_COUNT and len(set(stable_queue)) == 1:
                            if (
                                sent_last_weight is None or
                                abs(sent_last_weight - weight) >= SENSITIVITY_GRAM
                            ):
                                url = ODOO_URL_TEMPLATE.format(mrp_id=mrp_id, weight=weight)
                                try:
                                    resp = requests.get(url)
                                    print(f"Odoo'ya gönderildi: {weight} gr, Cevap: {resp.status_code}")
                                    sent_last_weight = weight
                                    stable_queue.clear()
                                except Exception as e:
                                    print("Odoo'ya gönderim hatası:", e)
        else:
            time.sleep(0.25)  # Veri akışı kapalıysa bekle

if __name__ == "__main__":
    main()
