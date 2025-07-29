import glob
import serial
import requests
import time
import re
from collections import deque

ODOO_URL_TEMPLATE = "http://localhost:9001/terazi/get/{mrp_id}/{weight}"
GET_JOB_URL = "http://localhost:9001/terazi/get_scale_job/1"  # scale_id'yi uygun gir!
STABLE_COUNT = 20
SENSITIVITY_GRAM = 4

def auto_serial_port_terazi():
    # Terazi için port bul (örnek: ttyUSB0, ttyUSB1, vs)
    ports = glob.glob('/dev/serial/by-id/usb*') + glob.glob('/dev/ttyUSB*')
    for port in ports:
        # Cihaz isminde terazi/ftdi/scale geçiyorsa seç, ya da ilk bulduğunu al
        if 'ftdi' in port.lower() or 'ad' in port.lower() or 'terazi' in port.lower():
            print("Terazi portu:", port)
            return port
    if ports:
        print("Terazi portu:", ports[0])
        return ports[0]
    raise Exception("Terazi cihazı bağlı değil!")

def auto_serial_port_yazici():
    # Yazıcı için port bul (örnek: ttyACM0, ttyACM1, vs)
    ports = glob.glob('/dev/ttyACM*') + glob.glob('/dev/serial/by-id/usb*')
    for port in ports:
        # Cihaz isminde topway/yazıcı/printer geçiyorsa seç, ya da ilk bulduğunu al
        if 'topway' in port.lower() or 'printer' in port.lower() or 'yazici' in port.lower():
            print("Yazıcı portu:", port)
            return port
    # Yedek: ilk ttyACM portu
    for port in ports:
        if 'ttyACM' in port:
            print("Yazıcı portu:", port)
            return port
    raise Exception("Yazıcı cihazı bağlı değil!")

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
    # Sadece 00000,XXX ile başlayan satırdaki XXX kısmı net ağırlık
    line = line.replace(b'\t', b'').replace(b'\x02', b'').replace(b'\x03', b'').replace(b'\n', b'').strip()
    match = re.search(br'00000,(\d{1,6})', line)
    if match:
        try:
            net_val = int(match.group(1))
            if net_val < 5:
                return None
            return net_val
        except:
            pass
    return None

def stable_value(stable_queue, value, tolerance):
    if len(stable_queue) < stable_queue.maxlen:
        return False
    min_val = min(stable_queue)
    max_val = max(stable_queue)
    return (max_val - min_val) <= tolerance

def send_scale_command(ser, cmd_byte, timeout=1.0):
    ser.write(cmd_byte)
    time.sleep(timeout)
    return True

def main():
    # Bağlı portları otomatik bul
    port_terazi = auto_serial_port_terazi()
    port_yazici = auto_serial_port_yazici()

    ser_terazi = serial.Serial(
        port=port_terazi,
        baudrate=19200,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_ODD,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.5,
    )
    ser_yazici = serial.Serial(
        port=port_yazici,
        baudrate=115200,  # Çoğu yazıcı için 9600, gerekirse değiştir!
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.5,
    )

    print("Hazır, 'Start' komutunu bekliyor...")

    buffer = b""
    sent_last_weight = None
    stable_queue = deque(maxlen=STABLE_COUNT)
    last_action_id = None
    sending_data = False

    while True:
        job_str, mrp_id = get_current_job()
        action_id = f"{job_str}:{mrp_id}"

        # Komut kontrolü (başlat, durdur, dara, sıfırla)
        if job_str and action_id != last_action_id:
            if "start" in job_str:
                print("START KOMUTU: Tartıdan veri akışı başlatılıyor...")
                send_scale_command(ser_terazi, b'\x02\x52\x4E\x1C\x03')
                sending_data = True
                last_action_id = action_id
            elif "done" in job_str:
                print("DONE KOMUTU: Tartıdan veri akışı durduruluyor...")
                sending_data = False
                last_action_id = action_id
            elif "tare" in job_str:
                print("DARA KOMUTU gönderiliyor...")
                send_scale_command(ser_terazi, b'\x02T\x03')
                last_action_id = action_id
            elif "zero" in job_str:
                print("SIFIRLA KOMUTU gönderiliyor...")
                send_scale_command(ser_terazi, b'\x02Z\x03')
                last_action_id = action_id

        # Veri akışı açıkken
        if sending_data and mrp_id:
            ser_terazi.write(b'\x02N\x1C\x03')
            chunk = ser_terazi.read(128)
            if chunk:
                buffer += chunk
                while b"\r" in buffer:
                    line, buffer = buffer.split(b"\r", 1)
                    weight = parse_weight_line(line)
                    if weight is not None:
                        stable_queue.append(weight)
                        if stable_value(stable_queue, weight, SENSITIVITY_GRAM):
                            if (sent_last_weight is None or abs(sent_last_weight - weight) >= SENSITIVITY_GRAM):
                                url = ODOO_URL_TEMPLATE.format(mrp_id=mrp_id, weight=weight)
                                try:
                                    resp = requests.get(url)
                                    print(f"Odoo'ya gönderildi: {weight} gr, Cevap: {resp.status_code}")
                                    sent_last_weight = weight
                                    stable_queue.clear()
                                    # Odoo'dan gelen response doğrudan yazıcıya gönder
                                    label_content = resp.text
                                    if label_content:
                                        print("Etiket yazıcıya gönderiliyor...")
                                        ser_yazici.write(label_content.encode("utf-8"))
                                        ser_yazici.flush()
                                except Exception as e:
                                    print("Odoo/etiket gönderim hatası:", e)
        else:
            time.sleep(0.25)

if __name__ == "__main__":
    main()
