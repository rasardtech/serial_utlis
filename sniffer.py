import serial

ser = serial.Serial('/dev/ttyACM0', baudrate=19200, parity='E', timeout=1)
while True:
    data = ser.read(128)
    if data:
        print(data.hex())