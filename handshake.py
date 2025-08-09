import serial
import time

def yazici_handshake(ser):
    cmds = [
        b"\x1b@\x1b@\x1b@\x1b@\x1b@\xaaU",
        b"\x1b=\x01",
        b"\x12\x45\x01",
        b"\x12\x70\x03\x00",
    ]
    for cmd in cmds:
        ser.write(cmd)
        ser.flush()
        time.sleep(0.05)

def yaziciya_text_gonder(port, text):
    with serial.Serial(
        port,
        baudrate=19200,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_ODD,
        stopbits=serial.STOPBITS_ONE,
        timeout=2
    ) as ser:
        yazici_handshake(ser)
        # ESC @ : initialize
        ser.write(b'\x1b@\n')
        # ESC E : bold on, ESC E off: bold off
        ser.write(b'\x1bE' + text.encode('ascii', errors='replace') + b'\n')
        # Feed and cut (bazı yazıcılar için)
        ser.write(b'\n\n\n')
        ser.flush()
    print("Metin yazıcıya gönderildi.")

# Kullanım:
yaziciya_text_gonder("/dev/ttyACM2", "Deneme Yazısı\nAğırlık: 1,000 KG\nS.T.T.: 29.11.2025")