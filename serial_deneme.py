import serial
import time

while True:
    try:
        ser = serial.Serial('/dev/ttyUSB0', 19200, timeout=1)
        # Başlangıçta RN komutu gönder
        ser.write(b'\x13')  # Xoff
        frame = make_ad2k_frame(b'RN\x1C')
        ser.write(frame)
        ser.write(b'\x11')  # Xon
        ser.flush()
        while True:
            data = ser.read(128)
            print(data)
    except serial.SerialException:
        print("Bağlantı koptu, yeniden denenecek...")
        time.sleep(2)
