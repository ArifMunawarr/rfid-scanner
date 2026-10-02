import os
import socket
import threading
import time
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# Global Settings
# ============================================================
MODE = os.getenv("RFID_MODE", "client")
DEFAULT_PORT = int(os.getenv("RFID_PORT", "5000"))
LISTEN_HOST = os.getenv("RFID_LISTEN_HOST", "0.0.0.0")
TAG_MIN = os.getenv("RFID_TAG_MIN", "")
TAG_MAX = os.getenv("RFID_TAG_MAX", "")

# ============================================================
# Build list of readers from .env
# Membaca RFID_READER_1_HOST ... RFID_READER_N_HOST secara otomatis
# ============================================================
def _load_readers():
    readers = []
    i = 1
    while True:
        host = os.getenv(f"RFID_READER_{i}_HOST")
        if not host:
            break
        port = int(os.getenv(f"RFID_READER_{i}_PORT", str(DEFAULT_PORT)))
        readers.append({"id": i, "host": host, "port": port})
        i += 1

    # Fallback ke RFID_READER_HOST lama jika tidak ada multi-reader di .env
    if not readers:
        host = os.getenv("RFID_READER_HOST", "172.15.5.222")
        readers.append({"id": 1, "host": host, "port": DEFAULT_PORT})

    return readers

READERS = _load_readers()
NUM_READERS = len(READERS)

# ============================================================
# Per-reader state
# ============================================================
# readers_state[reader_id] = { conn, running, buffer, tags, connection_status, status_lock, server }
readers_state = {}

for _r in READERS:
    _rid = _r["id"]
    readers_state[_rid] = {
        "conn": None,
        "server": None,
        "running": False,
        "buffer": b"",
        "tags": {},
        "status_lock": threading.Lock(),
        "connection_status": {
            "reader_id": _rid,
            "host": _r["host"],
            "port": _r["port"],
            "connected": False,
            "state": "disconnected",
            "message": "Waiting for RFID device",
            "client_address": None,
            "connected_at": None,
            "last_data_at": None,
            "last_error": None,
        },
    }

# Global scan flag (berlaku untuk semua reader)
is_scanning = True

# Gabungan semua tags dari semua reader (untuk backward-compat di routes.py)
tags = {}

# ============================================================
# Helpers
# ============================================================
def current_time_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _set_status(reader_id, **kwargs):
    state = readers_state[reader_id]
    with state["status_lock"]:
        state["connection_status"].update(kwargs)


def _get_status(reader_id):
    state = readers_state[reader_id]
    with state["status_lock"]:
        status = dict(state["connection_status"])
    rtags = state["tags"]
    active = sum(1 for t in rtags.values() if not t["remove"])
    status["active_tags"] = active
    status["total_tags"] = len(rtags)
    return status


def get_connection_status():
    """Mengembalikan status koneksi semua reader."""
    all_status = []
    total_active = 0
    total_tags = 0
    for r in READERS:
        s = _get_status(r["id"])
        all_status.append(s)
        total_active += s["active_tags"]
        total_tags += s["total_tags"]
    return {
        "readers": all_status,
        "total_readers": NUM_READERS,
        "active_tags": total_active,
        "total_tags": total_tags,
    }

def _sync_global_tags():
    """Sinkronisasi tags gabungan dari semua reader ke dict `tags` global."""
    global tags
    merged = {}
    for r in READERS:
        for epc, tag in readers_state[r["id"]]["tags"].items():
            key = f"{r['id']}:{epc}"
            merged[key] = {**tag, "reader_id": r["id"]}
    tags = merged

# =========================
# EXTRACT FRAME
# =========================
def _extract_frames(reader_id):
    state = readers_state[reader_id]
    buf = state["buffer"]
    frames = []

    while True:
        start = buf.find(b'\xbb')
        end = buf.find(b'\x0d\x0a')

        if start == -1 or end == -1 or end < start:
            break

        frame = buf[start:end + 2]
        buf = buf[end + 2:]
        frames.append(frame)

    state["buffer"] = buf
    return frames


# =========================
# PARSE FRAME
# =========================
def parse_frame(frame: bytes):
    try:
        if len(frame) < 15:
            return None

        if frame[1] != 0xF1:
            return None

        tag_id = frame[4:9].hex()
        device_id = frame[9:16].hex()

        power = frame[3]
        battery = "battereNormal"

        return {
            "id": tag_id,
            "epc": tag_id,
            "tid": None,
            "device_id": device_id,
            "power": power,
            "battery": battery,
            "raw": frame.hex(" ")
        }

    except Exception as e:
        print("Parse error:", e)
        return None


# =========================
# UPDATE TAG
# =========================
def _update_tag(reader_id, data):
    if not is_scanning:
        return

    rtags = readers_state[reader_id]["tags"]
    epc = data["id"]
    if not epc:
        return

    # Filter Range Tag ID
    if TAG_MIN and epc < TAG_MIN:
        return
    if TAG_MAX and epc > TAG_MAX:
        return

    now = time.time()

    if epc not in rtags:
        rtags[epc] = {
            "id": epc,
            "epc": data.get("epc"),
            "tid": data.get("tid"),
            "device_id": data["device_id"],
            "power": data["power"],
            "battery": data["battery"],
            "raw": data.get("raw"),
            "count": 1,
            "last_time": now,
            "remove": 0,
            "reader_id": reader_id,
        }
    else:
        rtags[epc]["epc"] = data.get("epc")
        rtags[epc]["tid"] = data.get("tid")
        rtags[epc]["raw"] = data.get("raw")
        rtags[epc]["count"] += 1
        rtags[epc]["last_time"] = now
        rtags[epc]["remove"] = 0

    _sync_global_tags()


# =========================
# CLEANUP (REMOVE)
# =========================
def cleanup_tags():
    while True:
        if is_scanning:
            now = time.time()
            for r in READERS:
                rtags = readers_state[r["id"]]["tags"]
                for epc in list(rtags.keys()):
                    if now - rtags[epc]["last_time"] > 5:
                        rtags[epc]["remove"] = 1
            _sync_global_tags()
        time.sleep(1)


# =========================
# FORMAT OUTPUT (terminal)
# =========================
def format_output(reader_id):
    if not is_scanning:
        return "[SCAN] Stopped"

    rtags = readers_state[reader_id]["tags"]
    lines = []
    header = f"[Reader {reader_id}] num   id           devid            remove power battery        count  lasttime"
    lines.append(header)

    i = 1
    for tag in rtags.values():
        dt = datetime.fromtimestamp(tag["last_time"])
        timestr = dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        line = f"{i:05d} {tag['id']:12s} {tag['device_id']:16s} {tag['remove']}      {tag['power']:5d} {tag['battery']:14s} {tag['count']:06d} {timestr}"
        lines.append(line)
        i += 1

    return "\n".join(lines)


# =========================
# SEND COMMAND (per-reader)
# =========================
def _send_command_to(reader_id, hex_cmd):
    state = readers_state[reader_id]
    conn = state["conn"]
    if not conn:
        return False
    try:
        cmd = bytes.fromhex(hex_cmd.replace(" ", ""))
        conn.sendall(cmd)
        print(f"[TCP][Reader {reader_id}] Command sent: {hex_cmd}")
        return True
    except Exception as e:
        _set_status(
            reader_id,
            connected=False,
            state="disconnected",
            message="Failed to send command",
            last_error=str(e),
        )
        return False


def send_command(hex_cmd):
    """Kirim command ke semua reader yang terkoneksi."""
    results = []
    for r in READERS:
        results.append(_send_command_to(r["id"], hex_cmd))
    return any(results)


# =========================
# TCP WORKER per-reader (auto-reconnect)
# =========================
def _tcp_worker(reader_id, host, port):
    state = readers_state[reader_id]

    while True:
        try:
            if MODE == "client":
                _set_status(
                    reader_id,
                    connected=False,
                    state="connecting",
                    message=f"Connecting to RFID device at {host}:{port}",
                )
                print(f"[TCP][Reader {reader_id}] Connecting to {host}:{port}...")
                conn = socket.create_connection((host, port), timeout=5)
                state["conn"] = conn
                _set_status(
                    reader_id,
                    connected=True,
                    state="connected",
                    message="Connected to RFID device",
                    client_address=f"{host}:{port}",
                    connected_at=current_time_str(),
                    last_error=None,
                )
                print(f"[TCP][Reader {reader_id}] Connected to {host}:{port}")
                _send_command_to(reader_id, "BB 17 02 00 00 19 0D 0A")
            else:
                srv = state.get("server")
                if srv:
                    try:
                        srv.close()
                    except:
                        pass

                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind((LISTEN_HOST, port))
                srv.listen(1)
                state["server"] = srv

                _set_status(
                    reader_id,
                    connected=False,
                    state="listening",
                    message="Listening for RFID device",
                    client_address=None,
                )
                print(f"[TCP][Reader {reader_id}] Listening on {port}... waiting for device")

                conn, addr = srv.accept()
                state["conn"] = conn
                _set_status(
                    reader_id,
                    connected=True,
                    state="connected",
                    message="Connected to RFID device",
                    client_address=f"{addr[0]}:{addr[1]}",
                    connected_at=current_time_str(),
                    last_error=None,
                )
                print(f"[TCP][Reader {reader_id}] Connected from {addr}")

            state["running"] = True
            state["buffer"] = b""

            while state["running"]:
                data = state["conn"].recv(1024)
                if not data:
                    _set_status(
                        reader_id,
                        connected=False,
                        state="disconnected",
                        message="RFID device disconnected",
                    )
                    print(f"[TCP][Reader {reader_id}] Device disconnected (empty read)")
                    break

                _set_status(reader_id, last_data_at=current_time_str())
                state["buffer"] += data

                frames = _extract_frames(reader_id)

                for f in frames:
                    if f[1] != 0xF1:
                        continue

                    parsed = parse_frame(f)
                    if not parsed:
                        continue

                    _update_tag(reader_id, parsed)

                    print(format_output(reader_id))
                    print("=" * 80)

        except Exception as e:
            _set_status(
                reader_id,
                connected=False,
                state="error",
                message="TCP error",
                last_error=str(e),
            )
            print(f"[TCP][Reader {reader_id}] Error: {e}")

        finally:
            state["running"] = False
            conn = state.get("conn")
            if conn:
                try:
                    conn.close()
                except:
                    pass
                state["conn"] = None
            _set_status(reader_id, connected=False, client_address=None)

        print(f"[TCP][Reader {reader_id}] Reconnecting in 3 seconds...")
        time.sleep(3)


def tcp_server():
    """Jalankan semua TCP worker secara paralel (1 thread per reader)."""
    threads = []
    for r in READERS:
        t = threading.Thread(
            target=_tcp_worker,
            args=(r["id"], r["host"], r["port"]),
            daemon=True,
            name=f"rfid-reader-{r['id']}",
        )
        t.start()
        threads.append(t)
        print(f"[RFID] Reader {r['id']} thread started: {r['host']}:{r['port']}")
    # Tunggu semua thread (tidak akan selesai karena infinite loop)
    for t in threads:
        t.join()

# =========================
# START / STOP SCAN CONTROLS
# =========================
def start_scan():
    global is_scanning
    is_scanning = True
    print("[SCAN] Start scan initiated (all readers)")
    return send_command("BB 17 02 00 00 19 0D 0A")

def stop_scan():
    global is_scanning, tags
    is_scanning = False
    for r in READERS:
        readers_state[r["id"]]["tags"].clear()
    tags.clear()
    print("[SCAN] Stop scan initiated (tags cleared on all readers)")
    return send_command("BB 18 00 18 0D 0A")
