import serial
import time

# Constants for AD-2000 protocol
STX = 0x02
ETX = 0x03
XON = 0x11
XOFF = 0x13

# BCC calculation with substitution rules
def calculate_bcc(data: bytes) -> int:
    bcc = 0
    for b in data:
        bcc ^= b
    if bcc == 0x00:
        return 0x20
    elif bcc == 0x11:
        return 0x31
    elif bcc == 0x13:
        return 0x33
    elif bcc == 0x02:
        return 0x22
    elif bcc == 0x03:
        return 0x23
    return bcc

# Frame construction
def build_frame(cmd_type: bytes, cmd: bytes, data: bytes = b'') -> bytes:
    body = cmd_type + cmd + data
    bcc = calculate_bcc(body)
    return bytes([STX]) + body + bytes([ETX]) + bytes([bcc])

# Send command frame and read response
def send_ad_command(ser, cmd_type: bytes, cmd: bytes, data: bytes = b''):
    ser.write(bytes([XOFF]))
    time.sleep(0.02)

    frame = build_frame(cmd_type, cmd, data)
    print(f"📤 Sending frame: {frame.hex(' ')}")
    ser.write(frame)

    # Read response
    response = bytearray()
    start_found = False
    start = time.time()
    while time.time() - start < 1.0:
        b = ser.read(1)
        if not b:
            continue
        byte = b[0]
        if not start_found:
            if byte == STX:
                start_found = True
                response.append(byte)
        else:
            response.append(byte)
            if byte == ETX:
                bcc = ser.read(1)
                if bcc:
                    response.append(bcc[0])
                break

    ser.write(bytes([XON]))

    if len(response) < 4:
        print("⚠️ Incomplete response.")
        return response

    # Check BCC
    body = response[1:-2]  # Exclude STX and ETX
    expected_bcc = calculate_bcc(body)
    actual_bcc = response[-1]

    if expected_bcc != actual_bcc:
        print(f"⚠️ BCC mismatch: expected 0x{expected_bcc:02X}, got 0x{actual_bcc:02X}")
    else:
        print("✅ BCC OK")

    return bytes(response)

# Main function: sends RF and RW commands
def main():
    port = "/dev/ttyUSB0"
    ser = serial.Serial(
        port=port,
        baudrate=19200,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_EVEN,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.1
    )

    print("🔍 Requesting software version (RF)...")
    response = send_ad_command(ser, b'R', b'F')
    print("📦 Raw response:", response)
    try:
        if response and response[1:3] == b'0F':
            version = response[3:-2].decode(errors="ignore")
            print("📌 Software version:", version)
        elif response and response[1:2] == b'2':
            print("❌ RF command not supported")
        else:
            print("⚠️ No valid version info found.")
    except Exception as e:
        print("❌ Error while parsing version:", e)

    print("\n⚖️ Requesting weight data (RW)...")
    response = send_ad_command(ser, b'R', b'W')
    print("📦 Raw response:", response)
    try:
        if response and response[1:2] == b'0':
            data_str = response[2:-2].decode(errors="ignore")
            print("✅ Weight data:", data_str)
        elif response and response[1:2] == b'2':
            print("❌ RW command not supported")
        else:
            print("⚠️ No valid weight info found.")
    except Exception as e:
        print("❌ Error while parsing weight:", e)

    ser.close()

if __name__ == "__main__":
    main()
