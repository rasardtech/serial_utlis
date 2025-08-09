import serial
import time

ser = serial.Serial("/dev/ttyACM0", baudrate=19200, bytesize=8, parity="E", stopbits=1, timeout=2)

# Reset/init birden fazla gönder
for _ in range(3):
    ser.write(b"\x1b@")
    time.sleep(0.1)

# Test komutları
ser.write(b"SIZE 60 mm,25 mm\r\n")
ser.write(b"CLS\r\n")
ser.write(b"TEXT 50,50,\"3\",0,1,1,\"TSC TEST\"\r\n")
ser.write(b"PRINT 1\r\n")
ser.close()