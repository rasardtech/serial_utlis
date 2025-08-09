import serial

with serial.Serial("/dev/ttyACM2", baudrate=19200, bytesize=8, parity="E", stopbits=1, timeout=2) as ser:
    ser.write(b"SIZE 60 mm,25 mm\r\n")
    ser.write(b"GAP 2 mm,0 mm\r\n")
    ser.write(b"DIRECTION 0,0\r\n")
    ser.write(b"REFERENCE 0,0\r\n")
    ser.write(b"CLS\r\n")
    # Sadece metin
    ser.write(b'TEXT 50,50,"3",0,1,1,"Merhaba"\r\n')
    ser.write(b"PRINT 1,1\r\n")