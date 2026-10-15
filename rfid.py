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
TAG_ACTIVE_TIMEOUT = float(os.getenv("RFID_TAG_ACTIVE_TIMEOUT", "5"))

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

    if MODE == "server":
        endpoints = {(r["host"], r["port"]) for r in readers}
        if len(endpoints) != len(readers):
            raise ValueError("Setiap reader harus memiliki kombinasi IP dan port yang berbeda")
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
tags_lock = threading.RLock()

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
    with tags_lock:
        rtags = state["tags"]
        status["active_tags"] = sum(1 for t in rtags.values() if not t["remove"])
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
    with tags_lock:
        merged = {}
        for r in READERS:
            for epc, tag in readers_state[r["id"]]["tags"].items():
                key = f"{r['id']}:{epc}"
                merged[key] = {**tag, "reader_id": r["id"]}
        tags.clear()
        tags.update(merged)


def get_tags():
    with tags_lock:
        return [dict(tag) for tag in tags.values()]

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
    with tags_lock:
        _update_tag_locked(reader_id, data)


def _update_tag_locked(reader_id, data):
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
        with tags_lock:
            if is_scanning:
                now = time.time()
                for r in READERS:
                    rtags = readers_state[r["id"]]["tags"]
                    for epc in list(rtags.keys()):
                        if now - rtags[epc]["last_time"] > TAG_ACTIVE_TIMEOUT:
                            rtags[epc]["remove"] = 1
                _sync_global_tags()
        time.sleep(1)


# =========================
# FORMAT OUTPUT (terminal)
# =========================
def format_output(reader_id):
    if not is_scanning:
        return "[SCAN] Stopped"

    with tags_lock:
        rtags = {epc: dict(tag) for epc, tag in readers_state[reader_id]["tags"].items()}
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
def _tcp_worker(reader_id, host, port, conn=None):
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
                conn.settimeout(None)
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
                addr = conn.getpeername()
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
            if conn:
                try:
                    conn.close()
                except:
                    pass
            with state["status_lock"]:
                state["conn"] = None
                state["connection_status"].update(connected=False, client_address=None)

        if MODE == "server":
            return
        print(f"[TCP][Reader {reader_id}] Reconnecting in 3 seconds...")
        time.sleep(3)


def _tcp_listener(port, readers):
    while True:
        try:
            readers_by_host = {socket.gethostbyname(r["host"]): r for r in readers}
            if len(readers_by_host) != len(readers):
                raise OSError("IP reader pada port yang sama tidak boleh duplikat")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind((LISTEN_HOST, port))
                srv.listen(max(1, len(readers)))
                for r in readers:
                    state = readers_state[r["id"]]
                    state["server"] = srv
                    if state["conn"] is None:
                        _set_status(
                            r["id"], connected=False, state="listening",
                            message="Listening for RFID device", client_address=None,
                            last_error=None,
                        )
                print(f"[TCP] Listening on {LISTEN_HOST}:{port} for {len(readers)} reader(s)")
                while True:
                    conn, addr = srv.accept()
                    reader = readers_by_host.get(addr[0])
                    if reader is None:
                        print(f"[TCP] Rejected unconfigured reader from {addr}")
                        conn.close()
                        continue
                    state = readers_state[reader["id"]]
                    with state["status_lock"]:
                        if state["conn"] is not None:
                            conn.close()
                            continue
                        state["conn"] = conn
                    threading.Thread(
                        target=_tcp_worker,
                        args=(reader["id"], reader["host"], port, conn),
                        daemon=True,
                        name=f"rfid-reader-{reader['id']}",
                    ).start()
        except OSError as exc:
            for r in readers:
                if readers_state[r["id"]]["conn"] is None:
                    _set_status(
                        r["id"], connected=False, state="error",
                        message="TCP listener error", last_error=str(exc),
                    )
            print(f"[TCP] Listener {port} error: {exc}")
        finally:
            for r in readers:
                readers_state[r["id"]]["server"] = None
        time.sleep(3)


def tcp_server():
    """Jalankan semua TCP worker secara paralel (1 thread per reader)."""
    threads = []
    if MODE == "server":
        groups = {}
        for r in READERS:
            groups.setdefault(r["port"], []).append(r)
        workers = [(_tcp_listener, (port, readers), f"rfid-listener-{port}")
                   for port, readers in groups.items()]
    else:
        workers = [(_tcp_worker, (r["id"], r["host"], r["port"]), f"rfid-reader-{r['id']}")
                   for r in READERS]
    for target, args, name in workers:
        t = threading.Thread(target=target, args=args, daemon=True, name=name)
        t.start()
        threads.append(t)
        print(f"[RFID] {name} thread started")
    # Tunggu semua thread (tidak akan selesai karena infinite loop)
    for t in threads:
        t.join()

# =========================
# START / STOP SCAN CONTROLS
# =========================
def start_scan():
    global is_scanning
    with tags_lock:
        is_scanning = True
    print("[SCAN] Start scan initiated (all readers)")
    return send_command("BB 17 02 00 00 19 0D 0A")

def stop_scan():
    global is_scanning
    with tags_lock:
        is_scanning = False
        for r in READERS:
            readers_state[r["id"]]["tags"].clear()
        tags.clear()
    print("[SCAN] Stop scan initiated (tags cleared on all readers)")
    return send_command("BB 18 00 18 0D 0A")
