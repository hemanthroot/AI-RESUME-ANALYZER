import os
import socket
import qrcode
import subprocess
import time
from threading import Thread

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def print_qr_code(title, network_url, local_url):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(network_url)
    qr.make(fit=True)
    
    print("\n" + "="*50)
    print(f"[{title}] Scan this QR code to open:")
    print(f"Network URL: {network_url}")
    print(f"Local URL:   {local_url}")
    print("="*50 + "\n")
    
    # Print QR code to terminal
    qr.print_ascii(invert=True)

def run_flask():
    print("Starting Flask server...")
    subprocess.run(["python", "main.py"])

if __name__ == "__main__":
    ip = get_local_ip()
    port = 5000
    
    user_network = f"http://{ip}:{port}/"
    user_local = f"http://127.0.0.1:{port}/"
    
    admin_network = f"http://{ip}:{port}/admin/login"
    admin_local = f"http://127.0.0.1:{port}/admin/login"
    
    # Print the QR code for Users
    print_qr_code("CANDIDATES / USERS", user_network, user_local)
    
    # Print the QR code for Admins
    print_qr_code("ADMINISTRATORS", admin_network, admin_local)
    
    # Start the server
    run_flask()
