# =============================================================
#  launcher.py  —  mDNS registration + Streamlit launcher
#
#  Advertises the dashboard on the local network as
#  p2-dashboard.local (configurable via config.MDNS_HOSTNAME)
#  and then starts Streamlit as a subprocess.
#
#  Usage:  python launcher.py
# =============================================================

import socket
import subprocess
import sys
import signal

import config as cfg


def _get_local_ip():
    """Get the LAN IP address of this machine."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    from zeroconf import Zeroconf, ServiceInfo

    hostname = cfg.MDNS_HOSTNAME
    port = cfg.DASHBOARD_PORT
    local_ip = _get_local_ip()

    # Register mDNS service
    service_info = ServiceInfo(
        type_="_http._tcp.local.",
        name=f"Jetson Dashboard._http._tcp.local.",
        server=f"{hostname}.local.",
        port=port,
        addresses=[socket.inet_aton(local_ip)],
        properties={"path": "/", "description": "Jetson Nano Sensor Dashboard"},
    )

    zc = Zeroconf()
    zc.register_service(service_info)

    print("=" * 60)
    print("  Jetson Dashboard — Network Access")
    print("=" * 60)
    print()
    print(f"  mDNS hostname : http://{hostname}.local:{port}")
    print(f"  IP address    : http://{local_ip}:{port}")
    print(f"  Max sessions  : {cfg.MAX_CONNECTIONS}")
    print()
    print("  NOTE: Ensure port {port} is allowed through Windows Firewall.")
    print("        Control Panel > Windows Firewall > Allow an app")
    print()
    print("  Press Ctrl+C to stop.")
    print("=" * 60)
    print()

    # Launch Streamlit as a subprocess
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "streamlit", "run", "dashboard.py",
            "--server.port", str(port),
            "--server.address", "0.0.0.0",
            "--server.headless", "true",
        ],
        cwd=sys.path[0] or ".",
    )

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\nShutting down...")
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        print("Unregistering mDNS service...")
        zc.unregister_service(service_info)
        zc.close()
        print("Done.")


if __name__ == "__main__":
    main()
