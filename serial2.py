import glob
import serial
import requests
import time
import re
from collections import deque

ODOO_URL_TEMPLATE = "https://altinayet-stage-22335048.dev.odoo.com/terazi/get/{mrp_id}/{weight}"
GET_JOB_URL = "https://altinayet-stage-22335048.dev.odoo.com/terazi/get_scale_job/1"  # scale_id'yi uygun gir!
STABLE_COUNT = 20
SENSITIVITY_GRAM = 8

def auto_serial_port_terazi():
    ports = glob.glob('/dev/serial/by-id/usb*') + glob.glob('/dev/ttyUSB*')
    for port in ports:
        if 'ftdi' in port.lower() or 'ad' in port.lower() or 'terazi' in port.lower():
            print("Terazi portu:", port)
            return port
    if ports:
        print("Terazi portu:", ports[0])
        return ports[0]
    raise Exception("Terazi cihazı bağlı değil!")

def auto_serial_port_yazici():
    ports = glob.glob('/dev/ttyACM*') + glob.glob('/dev/serial/by-id/usb*')
    for port in ports:
        if 'topway' in port.lower() or 'printer' in port.lower() or 'yazici' in port.lower():
            print("Yazıcı portu:", port)
            return port
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
    """
    Sadece 00000X,YYY ile başlayanları net ağırlık olarak döndürür (gram cinsinden).
    40000X,YYY ile başlayan satırları ve diğerlerini ignore eder.
    """
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
    min_val = min(stable_queue)
    max_val = max(stable_queue)
    return (max_val - min_val) <= tolerance

def make_ad2k_frame(command_bytes):
    frame = b'\x02' + command_bytes + b'\x03'
    bcc = 0
    for b in frame:
        bcc ^= b
    return frame + bytes([bcc])

def send_ad2k_command(ser, command_bytes, response_timeout=1.0, read_bytes=256):
    ser.reset_input_buffer()
    ser.write(b'\x13')  # Xoff
    time.sleep(0.02)
    frame = make_ad2k_frame(command_bytes)
    print("Gönderilen frame:", frame.hex())
    ser.write(frame)
    time.sleep(0.02)
    ser.write(b'\x11')  # Xon
    ser.flush()
    time.sleep(response_timeout)
    # Tüm geleni oku
    resp = b""
    start = time.time()
    while time.time() - start < response_timeout:
        part = ser.read(ser.in_waiting or 1)
        if part:
            resp += part
        else:
            time.sleep(0.01)
    print("Gelen yanıt (hex):", resp.hex())
    return resp

def send_terazi_handshake(ser):
    # Handshake paketini gönderiyoruz (ilk 32 byte)
    handshake = bytes([
        0xd9, 0xd0, 0x23, 0x34, 0xe4, 0xf6, 0xe9, 0x49,
        0x9f, 0x1c, 0xe2, 0xd7, 0x95, 0x3c, 0xa8, 0xea,
        0x7d, 0x07, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00,
        0xa6, 0xaf, 0xd0, 0x62, 0x88, 0x05, 0xdc, 0x01
    ])
    ser.write(handshake)
    ser.flush()
    time.sleep(0.5)
    print("Terazi handshake gönderildi.")

def send_yazici_handshake(ser):
    """Yazıcıya açılışta gönderilecek komutlar (handshake/init)"""
    init_cmds = [
        b"\x1b@\x1b@\x1b@\x1b@\x1b@\xaaU",      # 5x ESC @, sonra 0xAA 0x55
        b"\x1b=\x01",                           # ESC = 1
        b"\x12\x45\x01",                        # 0x12 E 0x01
        b"\x12\x70\x03\x00"                     # 0x12 p 0x03 0x00
    ]
    for cmd in init_cmds:
        ser.write(cmd)
        ser.flush()
        time.sleep(0.05)
    print("Yazıcı handshake/init komutları gönderildi.")

def main():
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
        baudrate=9600,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_ODD,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.5,
    )

    # --- HANDSHAKE EKLENDİ ---
    send_terazi_handshake(ser_terazi)
    send_yazici_handshake(ser_yazici)

    print("Hazır, 'Start' komutunu bekliyor...")

    buffer = b""
    sent_last_weight = None
    stable_queue = deque(maxlen=STABLE_COUNT)
    last_action_id = None
    sending_data = False

    while True:
        job_str, mrp_id = get_current_job()
        action_id = f"{job_str}:{mrp_id}"

        # Komut kontrolü
        if job_str and action_id != last_action_id:
            if "start" in job_str:
                print("START KOMUTU: Tartıdan veri akışı başlatılıyor...")
                sending_data = True
                last_action_id = action_id
            elif "done" in job_str:
                print("DONE KOMUTU: Tartıdan veri akışı durduruluyor...")
                sending_data = False
                last_action_id = action_id
            elif "tare" in job_str:
                print("DARA KOMUTU gönderiliyor...")
                resp = send_ad2k_command(ser_terazi, b'T')
                print("DARA cevap:", resp)
                last_action_id = action_id
            elif "zero" in job_str:
                print("SIFIRLA KOMUTU gönderiliyor...")
                resp = send_ad2k_command(ser_terazi, b'Z')
                print("SIFIRLA cevap:", resp)
                last_action_id = action_id

        if sending_data and mrp_id:
            resp = send_ad2k_command(ser_terazi, b'RN\x1C')
            buffer += resp
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