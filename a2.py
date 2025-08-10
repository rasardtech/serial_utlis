import serial, time

PORT="/dev/ttyACM1"; BAUD=19200
S="ÇçĞğİıÖöŞşÜü 123"
ser=serial.Serial(PORT, BAUD, timeout=1)

with ser:
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
    ser.write(b"\x1B@")
    for n in range(0, 50):
        ser.write(b"\x1Bt" + bytes([n]))
        ser.write(f"[t={n}] ".encode("ascii", errors="ignore"))
        try:
            ser.write(S.encode("cp1254", errors="replace"))
        except:
            ser.write(S.encode("latin-1", errors="replace"))
        ser.write(b"\n")
        ser.flush()
        time.sleep(0.05)