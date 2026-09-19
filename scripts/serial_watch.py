"""Serial capture that survives replug: reopens the port on disconnect."""

import sys
import time

import serial

PORT = "/dev/ttyACM0"
END = time.time() + int(sys.argv[1]) if len(sys.argv) > 1 else time.time() + 140

s = None
while time.time() < END:
    try:
        if s is None or not s.is_open:
            try:
                s = serial.Serial(PORT, 115200, timeout=2)
            except serial.SerialException:
                time.sleep(0.5)
                continue
        line = s.readline()
        if line:
            sys.stdout.write(line.decode(errors="replace"))
            sys.stdout.flush()
    except serial.SerialException:
        try:
            s.close()
        except Exception:
            pass
        s = None
print("CAPTURE-END")