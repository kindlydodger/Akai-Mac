import socket
import time

CARDPUTER_IP = "192.168.4.1"
LOG_PORT = 8889
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

def log(msg):
    sock.sendto(msg.encode('utf-8'), (CARDPUTER_IP, LOG_PORT))

if __name__ == "__main__":
    log("Macro Filter Sweep Active")
    for step in range(0, 128, 16):
        log(f"Cutoff CC70 -> {step}")
        time.sleep(0.2)
    log("Macro Sweep Completed")
