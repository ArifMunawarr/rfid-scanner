#!/usr/bin/env python3
"""Dashboard web lokal untuk menjalankan dan memantau listen_rfid.py."""

import argparse
import ipaddress
import math
import os
import re
import signal
import subprocess
import sys
import threading
import webbrowser
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

try:
    from fastapi import FastAPI, HTTPException, Query, Request
    from fastapi.middleware.trustedhost import TrustedHostMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse
    from pydantic import BaseModel, Field
    import uvicorn
except ImportError as exc:
    raise SystemExit(
        "Dependency dashboard belum tersedia. Jalankan: "
        "python3 -m pip install fastapi uvicorn"
    ) from exc


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
LISTENER_PATH = SCRIPT_DIR / "listen_rfid.py"
ENV_PATH = PROJECT_ROOT / ".env"

TAG_EVENT_RE = re.compile(
    r"^\[(?P<time>[^]]+)]\s+"
    r"\[(?P<event>TAG #\d+|ACTIVE AGAIN x\d+|REPEAT x\d+)]\s+"
    r"ID=(?P<id>\S+)\s+RAW_ID=(?P<raw>\S+)\s+"
    r"SERIAL=(?P<serial>\S+)\s+READER=(?P<reader>\S+)\s+"
    r"RSSI=(?P<rssi>-?\d+)dBm\s+TAG_TX=(?P<tag_tx>\S+)\s+"
    r"BAT=(?P<battery>\S+)"
)
REMOVAL_EVENT_RE = re.compile(
    r"^\[(?P<time>[^]]+)]\s+\[(?:REMOVE|STALE)]\s+"
    r"ID=(?P<id>\S+)"
)
DISCONNECTED_EVENT_RE = re.compile(
    r"^\[(?P<time>[^]]+)]\s+\[DISCONNECTED]\s+"
    r"READER=(?P<reader>[^,\s]+)"
)
CAPACITY_EVENT_RE = re.compile(
    r"\[CAPACITY(?: FINAL)?\].*DROPPED_TOTAL=(?P<total>\d+)"
)
NUMBERED_READER_RE = re.compile(r"^RFID_READER_(\d+)_HOST$")
MAX_TRACKED_TAGS = 2000


def fallback_env_values(path):
    values = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def read_env_values(path=ENV_PATH):
    """Baca file proyek secara langsung agar perubahan IP segera terlihat."""
    try:
        from dotenv import dotenv_values

        return {
            key: str(value)
            for key, value in dotenv_values(path).items()
            if value is not None
        }
    except ImportError:
        return fallback_env_values(path)


def discover_readers(config):
    default_port = config.get("RFID_PORT", "5000").strip() or "5000"
    readers = []
    for key, raw_host in config.items():
        match = NUMBERED_READER_RE.fullmatch(key)
        host = raw_host.strip()
        if not match or not host:
            continue
        reader_id = int(match.group(1))
        port = config.get(f"RFID_READER_{reader_id}_PORT", default_port).strip()
        readers.append(
            {
                "id": reader_id,
                "name": f"Reader {reader_id}",
                "host": host,
                "port": port or default_port,
                "source": key,
            }
        )
    readers.sort(key=lambda reader: reader["id"])
    if readers:
        return readers

    legacy_host = config.get("RFID_READER_HOST", "").strip()
    if legacy_host:
        return [
            {
                "id": None,
                "name": "Reader utama",
                "host": legacy_host,
                "port": default_port,
                "source": "RFID_READER_HOST",
            }
        ]
    return []


def env_config_payload():
    config = read_env_values()
    readers = discover_readers(config)
    selected_id = config.get("RFID_READER_ID", "").strip()
    selected_index = 0
    if selected_id:
        for index, reader in enumerate(readers):
            if str(reader["id"]) == selected_id:
                selected_index = index
                break
    try:
        mtime_ns = ENV_PATH.stat().st_mtime_ns
    except OSError:
        mtime_ns = None
    return {
        "env_path": str(ENV_PATH),
        "env_mtime_ns": str(mtime_ns) if mtime_ns is not None else None,
        "mode": config.get("RFID_MODE", "client").strip().lower(),
        "listen_host": config.get("RFID_LISTEN_HOST", "0.0.0.0").strip(),
        "default_port": config.get("RFID_PORT", "5000").strip(),
        "active_timeout": config.get("RFID_TAG_ACTIVE_TIMEOUT", "5").strip(),
        "readers": readers,
        "selected_reader_index": selected_index,
    }


def build_listener_command(request):
    """Backend selalu emit repeat; browser boleh menyembunyikannya dari log."""
    command = [
        sys.executable,
        "-u",
        str(LISTENER_PATH),
        "--mode",
        request.mode,
        "--port",
        str(request.port),
        "--active-timeout",
        str(request.active_timeout),
        "--connect-timeout",
        str(request.connect_timeout),
        "--reconnect-delay",
        str(request.reconnect_delay),
        "--config-timeout",
        str(request.config_timeout),
        "--show-repeats",
    ]
    if request.mode == "client":
        command.extend(["--reader-ip", request.address])
    else:
        command.extend(["--host", request.address])
        command.extend(["--allowed-reader-ip", request.allowed_reader_ip])
    command.append(
        "--maximize-range"
        if request.maximize_range
        else "--no-maximize-range"
    )
    return command


def parse_listener_event(line):
    match = TAG_EVENT_RE.search(line)
    if match:
        data = match.groupdict()
        count_match = re.search(r" x(\d+)$", data["event"])
        data.update(
            {
                "type": "tag",
                "state": "ACTIVE",
                "count": int(count_match.group(1)) if count_match else 1,
                "rssi": int(data["rssi"]),
            }
        )
        return data

    match = REMOVAL_EVENT_RE.search(line)
    if match:
        return {"type": "remove", **match.groupdict()}

    match = DISCONNECTED_EVENT_RE.search(line)
    if match:
        return {"type": "disconnected", **match.groupdict()}

    match = CAPACITY_EVENT_RE.search(line)
    if match:
        return {"type": "capacity", "total": int(match.group("total"))}

    lower = line.lower()

    if "parameter runtime terverifikasi" in lower:
        return {
            "type": "rf",
            "state": "TX=7/RX=0 — READBACK COCOK; EFEK RF BELUM DIUJI",
        }
    if "sudah tx=7 dan rx attenuation=0" in lower:
        return {
            "type": "rf",
            "state": "TX=7/RX=0 TERBACA — EFEK RF BELUM DIUJI",
        }
    if "tx query config 2.4g" in lower:
        return {"type": "rf", "state": "MEMERIKSA PARAMETER"}
    if "config awal:" in lower:
        return {"type": "rf", "state": "PARAMETER TERBACA"}
    if "tx set max range" in lower:
        return {"type": "rf", "state": "MENERAPKAN PARAMETER"}
    if "tx verify config" in lower or "ack set tidak diterima" in lower:
        return {"type": "rf", "state": "MEMVERIFIKASI PARAMETER"}
    if any(
        text in lower
        for text in (
            "maximize-range dilewati",
            "stop tidak dikonfirmasi",
            "set rf dilewati",
            "gagal mengirim stop sebelum config",
        )
    ):
        return {"type": "rf", "state": "DILEWATI — SCAN LANJUT"}
    if any(
        text in lower
        for text in (
            "query konfigurasi 2.4g ditolak",
            "set runtime ditolak",
            "gagal mengirim set max range",
            "gagal mengirim query config",
            "gagal mengirim verify config",
            "receiver tidak mengembalikan konfigurasi 2.4g yang valid",
            "perubahan runtime tidak terkonfirmasi",
            "konfigurasi hasil query ulang tidak cocok",
            "query verifikasi ditolak",
            "respons query verifikasi tidak memiliki format yang valid",
        )
    ):
        return {"type": "rf", "state": "GAGAL — SCAN LANJUT"}

    if "menghubungkan ke receiver" in lower:
        return {"type": "connection", "state": "MENGHUBUNGKAN"}
    if "terhubung ke receiver" in lower or "receiver terhubung dari" in lower:
        return {"type": "connection", "state": "TERHUBUNG"}
    if "tx start continuous" in lower:
        return {"type": "connection", "state": "PERINTAH SCAN TERKIRIM"}
    if "menunggu receiver terhubung kembali" in lower:
        return {"type": "connection", "state": "MENUNGGU READER"}
    if "listening di" in lower or "menunggu rfid receiver" in lower:
        return {"type": "connection", "state": "MENUNGGU READER"}
    if "mencoba terhubung kembali" in lower:
        return {"type": "connection", "state": "MENCOBA ULANG"}
    if any(
        text in lower
        for text in (
            "receiver memutus koneksi",
            "koneksi reader terputus/gagal",
            "koneksi receiver berakhir",
        )
    ):
        return {"type": "connection", "state": "TERPUTUS"}
    return None


class ScanRequest(BaseModel):
    mode: Literal["client", "server"] = "client"
    address: str = Field(min_length=1, max_length=255)
    port: int = Field(default=5000, ge=1, le=65535)
    active_timeout: float = Field(default=5, gt=0, le=3600)
    connect_timeout: float = Field(default=5, gt=0, le=300)
    reconnect_delay: float = Field(default=3, gt=0, le=3600)
    config_timeout: float = Field(default=2, gt=0, le=300)
    maximize_range: bool = True
    allowed_reader_ip: str | None = None


def validate_scan_request(request):
    request.address = request.address.strip()
    if not request.address:
        raise HTTPException(status_code=422, detail="IP/bind address tidak boleh kosong")
    if request.mode == "server":
        request.allowed_reader_ip = (request.allowed_reader_ip or "").strip()
        if not request.allowed_reader_ip:
            raise HTTPException(
                status_code=422,
                detail="Mode server memerlukan IP reader yang diizinkan dari .env",
            )
    for name in (
        "active_timeout",
        "connect_timeout",
        "reconnect_delay",
        "config_timeout",
    ):
        if not math.isfinite(getattr(request, name)):
            raise HTTPException(status_code=422, detail=f"{name} harus finite")


class RFIDController:
    def __init__(self):
        self.lock = threading.RLock()
        self.process = None
        self.process_state = "STOPPED"
        self.connection_state = "IDLE"
        self.rf_state = "TIDAK DIMINTA"
        self.stop_requested = False
        self.started_at = None
        self.exit_code = None
        self.command = []
        self.tags = {}
        self.logs = deque(maxlen=2000)
        self.log_sequence = 0
        self.forced_kill = False
        self.capacity_dropped = 0
        self.total_reads = 0
        self.removed_tags = 0
        self.listener_capacity_total = 0
        self.stop_thread = None

    def _append_log_locked(self, line, kind=None):
        line = line.rstrip("\r\n")
        if not line:
            return
        self.log_sequence += 1
        if kind is None:
            kind = "repeat" if "[REPEAT " in line else "normal"
        self.logs.append(
            {"seq": self.log_sequence, "line": line, "kind": kind}
        )

    def append_ui_log(self, message):
        with self.lock:
            self._append_log_locked(f"[WEB] {message}", "web")

    def start(self, request):
        validate_scan_request(request)
        if not LISTENER_PATH.is_file():
            raise RuntimeError(f"Listener tidak ditemukan: {LISTENER_PATH}")
        command = build_listener_command(request)
        popen_kwargs = {
            "cwd": str(PROJECT_ROOT),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        with self.lock:
            if self.process and self.process.poll() is None:
                raise RuntimeError("Listener masih berjalan")
            try:
                process = subprocess.Popen(command, **popen_kwargs)
            except OSError as exc:
                self.process_state = "ERROR"
                self.connection_state = "GAGAL MEMULAI"
                self._append_log_locked(f"[WEB] Gagal menjalankan listener: {exc}")
                raise RuntimeError(str(exc)) from exc

            self.tags.clear()
            self.capacity_dropped = 0
            self.total_reads = 0
            self.removed_tags = 0
            self.listener_capacity_total = 0
            self.process = process
            self.process_state = "STARTING"
            self.connection_state = "MEMULAI"
            self.rf_state = (
                "MEMERIKSA" if request.maximize_range else "TIDAK DIMINTA"
            )
            self.stop_requested = False
            self.started_at = self._now_text()
            self.exit_code = None
            self.forced_kill = False
            self.stop_thread = None
            self.command = command
            self._append_log_locked(
                f"[WEB] START PID={process.pid} target={request.address}:{request.port}"
            )

        threading.Thread(
            target=self._read_process_output,
            args=(process,),
            daemon=True,
            name=f"rfid-listener-{process.pid}",
        ).start()

    @staticmethod
    def _now_text():
        from datetime import datetime

        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _read_process_output(self, process):
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    self._handle_listener_line(process, line)
        finally:
            return_code = process.wait()
            with self.lock:
                if self.process is not process:
                    return
                requested = self.stop_requested
                forced = self.forced_kill
                self.exit_code = return_code
                self.process = None
                if requested and forced:
                    self.process_state = "FORCED STOP"
                    self.connection_state = "FORCED STOP"
                elif requested:
                    self.process_state = "STOPPED"
                    self.connection_state = "STOPPED"
                else:
                    self.process_state = "ERROR"
                    self.connection_state = f"PROSES BERHENTI ({return_code})"
                self.removed_tags += len(self.tags)
                self.tags.clear()
                outcome = (
                    "forced"
                    if forced
                    else "stop-requested"
                    if requested
                    else "unexpected"
                )
                self._append_log_locked(
                    f"[WEB] Listener selesai ({outcome}), exit={return_code}"
                )

    def _handle_listener_line(self, process, line):
        event = parse_listener_event(line)
        with self.lock:
            if self.process is not process:
                return
            # Event timeout adalah sinyal internal untuk menghapus row. Jangan
            # masukkan ke deque log agar churn tag tidak menggusur log penting.
            if not event or event["type"] != "remove":
                self._append_log_locked(line)
            if self.process_state == "STARTING":
                self.process_state = "RUNNING"
            if not event:
                return
            event_type = event["type"]
            if event_type == "tag":
                if self.stop_requested:
                    return
                previous = self.tags.get(event["id"])
                self.total_reads += 1
                if previous is None and len(self.tags) >= MAX_TRACKED_TAGS:
                    self.capacity_dropped += 1
                    return
                best = (
                    event["rssi"]
                    if previous is None
                    else max(previous["best_rssi"], event["rssi"])
                )
                first_seen = event["time"] if previous is None else previous["first_seen"]
                self.tags[event["id"]] = {
                    "id": event["id"],
                    "raw": event["raw"],
                    "serial": event["serial"],
                    "state": "ACTIVE",
                    "rssi": event["rssi"],
                    "best_rssi": best,
                    "battery": event["battery"],
                    "tag_tx": event["tag_tx"],
                    "count": 1 if previous is None else previous["count"] + 1,
                    "first_seen": first_seen,
                    "last_seen": event["time"],
                    "reader": event["reader"],
                    "order": self.log_sequence,
                }
                self.connection_state = "SCANNING"
            elif event_type == "remove":
                if self.tags.pop(event["id"], None) is not None:
                    self.removed_tags += 1
            elif event_type == "disconnected":
                removed_ids = [
                    tag_id
                    for tag_id, tag in self.tags.items()
                    if tag["reader"] == event["reader"]
                ]
                for tag_id in removed_ids:
                    self.tags.pop(tag_id, None)
                self.removed_tags += len(removed_ids)
            elif event_type == "connection":
                self.connection_state = event["state"]
            elif event_type == "rf":
                self.rf_state = event["state"]
            elif event_type == "capacity":
                delta = max(0, event["total"] - self.listener_capacity_total)
                self.listener_capacity_total = max(
                    self.listener_capacity_total, event["total"]
                )
                self.capacity_dropped += delta
                self.total_reads += delta

    def stop(self, wait=False):
        with self.lock:
            process = self.process
            if not process or process.poll() is not None:
                return False
            already_requested = self.stop_requested
            if not already_requested:
                self.stop_requested = True
                self.process_state = "STOPPING"
                self.connection_state = "MENGHENTIKAN"
                self.removed_tags += len(self.tags)
                self.tags.clear()
                self._append_log_locked(
                    "[WEB] Menghentikan listener; menunggu cleanup STOP reader"
                )
                self.stop_thread = threading.Thread(
                    target=self._stop_process,
                    args=(process,),
                    daemon=True,
                    name=f"rfid-stop-{process.pid}",
                )
                self.stop_thread.start()
            stop_thread = self.stop_thread
        if wait and stop_thread:
            stop_thread.join(timeout=8)
            if stop_thread.is_alive():
                self._force_process_stop(process)
                stop_thread.join(timeout=2)
        return True

    def _stop_process(self, process):
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass

        self._force_process_stop(process)

    def _force_process_stop(self, process):
        with self.lock:
            if self.process is not process or process.poll() is not None:
                return
            try:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                return
            self.forced_kill = True
            self._append_log_locked(
                "[WEB] Timeout cleanup; listener dihentikan paksa"
            )
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    def clear(self):
        with self.lock:
            self.tags.clear()
            self.capacity_dropped = 0
            self.total_reads = 0
            self.removed_tags = 0
            self.logs.clear()
            self._append_log_locked(
                "[WEB] Riwayat dashboard dibersihkan; reader tidak diubah"
            )

    def snapshot(self, logs_after=0, show_repeats=False):
        with self.lock:
            running = bool(self.process and self.process.poll() is None)
            tags = [
                dict(tag)
                for tag in self.tags.values()
                if tag.get("state") == "ACTIVE"
            ]
            oldest_seq = self.logs[0]["seq"] if self.logs else self.log_sequence
            logs_reset = bool(logs_after and logs_after < oldest_seq - 1)
            logs = [
                dict(entry)
                for entry in self.logs
                if entry["seq"] > logs_after
                and (show_repeats or entry["kind"] != "repeat")
            ]
            process_state = self.process_state
            connection_state = self.connection_state
            rf_state = self.rf_state
            started_at = self.started_at
            exit_code = self.exit_code
            command = list(self.command)
            latest_log_seq = self.log_sequence
            capacity_dropped = self.capacity_dropped
            total_reads = self.total_reads
            removed_tags = self.removed_tags

        tags.sort(
            key=lambda tag: (
                -tag["order"],
                tag["id"],
            )
        )
        active = len(tags)
        return {
            "running": running,
            "process_state": process_state,
            "connection_state": connection_state,
            "rf_state": rf_state,
            "started_at": started_at,
            "exit_code": exit_code,
            "command": command,
            "stats": {
                "unique": len(tags),
                "active": active,
                "removed": removed_tags,
                "reads": total_reads,
                "capacity_dropped": capacity_dropped,
            },
            "tags": tags,
            "logs": logs,
            "logs_reset": logs_reset,
            "latest_log_seq": latest_log_seq,
        }


controller = RFIDController()


@asynccontextmanager
async def lifespan(_app):
    yield
    controller.stop(wait=True)


app = FastAPI(
    title="F2413 RFID Web Monitor",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "testserver"],
)


@app.middleware("http")
async def local_clients_only(request, call_next):
    client_host = request.client.host if request.client else ""
    if client_host == "testclient":
        is_loopback = True
    else:
        try:
            is_loopback = ipaddress.ip_address(client_host).is_loopback
        except ValueError:
            is_loopback = client_host == "localhost"
    if not is_loopback:
        return JSONResponse(
            status_code=403,
            content={"detail": "Dashboard hanya dapat diakses dari komputer ini"},
        )
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'"
    )
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


def require_same_origin(request):
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Origin dashboard tidak valid")


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


@app.get("/api/config")
def get_config():
    return env_config_payload()


@app.get("/api/state")
def get_state(
    logs_after: int = Query(default=0, ge=0),
    show_repeats: bool = False,
):
    return controller.snapshot(logs_after, show_repeats)


@app.post("/api/start")
def start_scan(payload: ScanRequest, request: Request):
    require_same_origin(request)
    try:
        controller.start(payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return controller.snapshot()


@app.post("/api/stop")
def stop_scan(request: Request):
    require_same_origin(request)
    requested = controller.stop()
    return {"stop_requested": requested}


@app.post("/api/clear")
def clear_dashboard(request: Request):
    require_same_origin(request)
    controller.clear()
    return {"cleared": True}


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "listener_exists": LISTENER_PATH.is_file(),
        "env_exists": ENV_PATH.is_file(),
    }


DASHBOARD_HTML = r"""
<!doctype html>
<html lang="id">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>F2413 RFID Monitor</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #07101d;
      --panel: #0e1a2b;
      --panel-2: #122238;
      --line: #203754;
      --text: #e8f0fb;
      --muted: #8fa5c1;
      --green: #39d6ad;
      --green-bg: #123c37;
      --yellow: #f5ca6a;
      --yellow-bg: #40351d;
      --red: #ff8290;
      --red-bg: #48242e;
      --blue: #69aefc;
      --shadow: 0 18px 50px rgba(0, 0, 0, .24);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at 85% -10%, rgba(43, 118, 155, .18), transparent 34%),
        radial-gradient(circle at -5% 8%, rgba(37, 155, 126, .12), transparent 30%),
        var(--bg);
      color: var(--text);
      font: 14px/1.45 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    button, input, select { font: inherit; }
    .app { width: min(1500px, calc(100% - 36px)); margin: 0 auto; padding: 26px 0 36px; }
    header { display: flex; align-items: center; gap: 18px; margin-bottom: 18px; }
    .brand { flex: 1; }
    h1 { margin: 0; letter-spacing: -.03em; font-size: clamp(25px, 3vw, 36px); }
    .subtitle { color: var(--muted); margin-top: 4px; }
    .pill {
      display: inline-flex; align-items: center; gap: 8px; border-radius: 999px;
      padding: 9px 14px; background: #1a2b43; color: #c4d3e6; font-weight: 750;
      letter-spacing: .03em; white-space: nowrap;
    }
    .pill::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: currentColor; box-shadow: 0 0 13px currentColor; }
    .pill.good { color: var(--green); background: var(--green-bg); }
    .pill.wait { color: var(--yellow); background: var(--yellow-bg); }
    .pill.bad { color: var(--red); background: var(--red-bg); }
    .panel { background: linear-gradient(145deg, rgba(17, 33, 54, .96), rgba(12, 25, 42, .96)); border: 1px solid rgba(63, 91, 126, .34); border-radius: 16px; box-shadow: var(--shadow); }
    .config { padding: 18px; margin-bottom: 14px; }
    .panel-title { font-size: 12px; font-weight: 800; letter-spacing: .12em; color: #a9bad0; text-transform: uppercase; margin-bottom: 13px; }
    .config-grid { display: grid; grid-template-columns: .65fr 1.55fr 1.2fr .55fr .65fr; gap: 12px; align-items: end; }
    label { display: grid; gap: 6px; color: var(--muted); font-size: 12px; font-weight: 650; }
    input, select {
      width: 100%; min-height: 41px; border: 1px solid var(--line); border-radius: 9px;
      background: #0a1728; color: var(--text); padding: 8px 10px; outline: none;
    }
    input:focus, select:focus { border-color: #3f9d93; box-shadow: 0 0 0 3px rgba(57, 214, 173, .08); }
    input:disabled, select:disabled { opacity: .58; cursor: not-allowed; }
    .config-row { display: flex; align-items: center; flex-wrap: wrap; gap: 14px; margin-top: 14px; }
    .check { display: inline-flex; grid-template-columns: auto 1fr; align-items: center; gap: 8px; color: var(--text); font-size: 13px; cursor: pointer; }
    .check input { width: 17px; min-height: 17px; accent-color: var(--green); }
    details { margin-top: 12px; color: var(--muted); }
    summary { cursor: pointer; user-select: none; font-weight: 700; }
    .advanced { display: grid; grid-template-columns: repeat(3, minmax(110px, 180px)); gap: 12px; margin-top: 10px; }
    .note { margin-top: 12px; padding: 10px 12px; border-left: 3px solid var(--yellow); border-radius: 7px; background: rgba(245, 202, 106, .07); color: #d3c297; font-size: 12px; }
    .toolbar { display: flex; align-items: center; gap: 9px; margin-top: 14px; }
    .env-path { min-width: 0; flex: 1; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
    button { border: 0; border-radius: 9px; min-height: 40px; padding: 9px 15px; cursor: pointer; font-weight: 800; transition: .16s ease; }
    button:hover:not(:disabled) { transform: translateY(-1px); filter: brightness(1.08); }
    button:disabled { opacity: .45; cursor: not-allowed; }
    .primary { background: var(--green); color: #031814; }
    .danger { background: #542733; color: #ffb4bd; }
    .secondary { background: #182c46; color: #cbd8e9; border: 1px solid #29425f; }
    .command { margin: 0 0 14px; display: flex; align-items: center; gap: 12px; padding: 11px 14px; color: var(--muted); }
    .command strong { color: #b6c7dc; font-size: 11px; letter-spacing: .1em; }
    .command code { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #a9bdd6; }
    .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 14px; }
    .stat { padding: 15px; min-height: 93px; }
    .stat .name { color: var(--muted); font-size: 10px; font-weight: 850; letter-spacing: .11em; text-transform: uppercase; }
    .stat .value { margin-top: 7px; font-size: clamp(20px, 2.3vw, 29px); font-weight: 850; letter-spacing: -.03em; }
    .stat .value.green { color: var(--green); }
    .stat .value.yellow { color: var(--yellow); }
    .stat .value.blue { color: var(--blue); }
    .stat.rf .value { font-size: 14px; line-height: 1.35; letter-spacing: 0; }
    .content { display: grid; grid-template-columns: minmax(0, 1.65fr) minmax(320px, .75fr); gap: 14px; }
    .table-panel, .log-panel { min-height: 490px; overflow: hidden; }
    .section-head { min-height: 58px; padding: 13px 15px; display: flex; align-items: center; gap: 12px; border-bottom: 1px solid var(--line); }
    .section-head h2 { margin: 0; font-size: 14px; letter-spacing: .04em; }
    .section-head .spacer { flex: 1; }
    .search { max-width: 240px; min-height: 35px; }
    .table-wrap { overflow: auto; max-height: 540px; }
    table { width: 100%; border-collapse: collapse; white-space: nowrap; }
    th { position: sticky; top: 0; z-index: 1; background: #142640; color: #aebed2; padding: 10px 11px; text-align: left; font-size: 10px; letter-spacing: .08em; }
    td { padding: 10px 11px; border-bottom: 1px solid rgba(43, 67, 96, .48); color: #c9d6e7; font-variant-numeric: tabular-nums; }
    tr:hover td { background: rgba(36, 65, 98, .18); }
    .tag-id { color: #f0f5fb; font-weight: 750; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    .state { display: inline-flex; border-radius: 999px; padding: 4px 8px; font-size: 10px; font-weight: 850; }
    .state.ACTIVE { color: var(--green); background: var(--green-bg); }
    .state.DISCONNECTED { color: var(--red); background: var(--red-bg); }
    .empty { text-align: center; color: var(--muted); padding: 70px 20px !important; }
    .console { height: 540px; overflow: auto; padding: 12px; background: #050d18; font: 11px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .log-line { color: #9fb4cd; white-space: pre-wrap; overflow-wrap: anywhere; padding: 1px 0; }
    .log-line.repeat { color: #607a98; }
    .log-line.web { color: #72d6bd; }
    .toast { position: fixed; right: 20px; bottom: 20px; max-width: min(420px, calc(100% - 40px)); padding: 12px 15px; border-radius: 10px; background: #18334a; color: #e7f1fc; box-shadow: var(--shadow); opacity: 0; transform: translateY(14px); pointer-events: none; transition: .2s ease; }
    .toast.show { opacity: 1; transform: translateY(0); }
    .toast.error { background: #542733; color: #ffd4d9; }
    @media (max-width: 1080px) {
      .config-grid { grid-template-columns: repeat(3, 1fr); }
      .stats { grid-template-columns: repeat(2, 1fr); }
      .content { grid-template-columns: 1fr; }
      .table-panel, .log-panel { min-height: 0; }
      .console { height: 330px; }
    }
    @media (max-width: 680px) {
      .app { width: min(100% - 20px, 1500px); padding-top: 16px; }
      header { align-items: flex-start; }
      .config-grid, .advanced, .stats { grid-template-columns: 1fr 1fr; }
      .toolbar { align-items: stretch; }
      .env-path { display: none; }
      .command { display: none; }
      .section-head { flex-wrap: wrap; }
      .search { max-width: none; width: 100%; }
    }
  </style>
</head>
<body>
  <main class="app">
    <header>
      <div class="brand">
        <h1>F2413 RFID Monitor</h1>
        <div class="subtitle">Dashboard continuous scan • hanya tag yang aktif saat ini</div>
      </div>
      <div id="connectionPill" class="pill">IDLE</div>
    </header>

    <section class="panel config">
      <div class="panel-title">Konfigurasi listener</div>
      <div class="config-grid">
        <label>Mode
          <select id="mode"><option value="client">Client</option><option value="server">Server</option></select>
        </label>
        <label>Reader dari .env
          <select id="reader"></select>
        </label>
        <label><span id="addressLabel">IP reader</span>
          <input id="address" autocomplete="off" placeholder="172.15.0.80">
        </label>
        <label>Port
          <input id="port" type="number" min="1" max="65535" value="5000">
        </label>
        <label>Hapus jika tidak terbaca (detik)
          <input id="activeTimeout" type="number" min="0.1" step="0.1" value="5">
        </label>
      </div>
      <div class="config-row">
        <label class="check"><input id="maximize" type="checkbox" checked> Maksimalkan TX/RX reader</label>
        <label class="check"><input id="showRepeats" type="checkbox"> Tampilkan REPEAT di event log</label>
      </div>
      <details>
        <summary>Pengaturan lanjutan</summary>
        <div class="advanced">
          <label>Connect timeout (s)<input id="connectTimeout" type="number" min="0.1" step="0.1" value="5"></label>
          <label>Reconnect delay (s)<input id="reconnectDelay" type="number" min="0.1" step="0.1" value="3"></label>
          <label>Config timeout (s)<input id="configTimeout" type="number" min="0.1" step="0.1" value="2"></label>
        </div>
      </details>
      <div class="note">Daftar bersifat active-only: tag otomatis hilang jika tidak ada frame baru selama batas waktu di atas, saat reader terputus, atau saat scan dihentikan. Mode maksimum aktif secara default dan mencoba register TX=7/RX attenuation=0 sesuai SDK. Readback yang cocok bukan bukti jangkauan fisik maksimum; efek F2413 tetap harus diuji.</div>
      <div class="toolbar">
        <span id="envPath" class="env-path">Memuat .env…</span>
        <button id="reloadEnv" class="secondary">Muat ulang .env</button>
        <button id="start" class="primary">Mulai Scan</button>
        <button id="stop" class="danger" disabled>Stop</button>
      </div>
    </section>

    <section class="panel command"><strong>COMMAND</strong><code id="command">Listener belum dijalankan</code></section>

    <section class="stats">
      <article class="panel stat"><div class="name">Tag aktif saat ini</div><div id="unique" class="value green">0</div></article>
      <article class="panel stat"><div class="name">Dikeluarkan dari daftar</div><div id="removed" class="value yellow">0</div></article>
      <article class="panel stat"><div class="name">Frame diproses dashboard</div><div id="reads" class="value blue">0</div></article>
      <article class="panel stat rf"><div class="name">Status RF</div><div id="rf" class="value">TIDAK DIMINTA</div></article>
    </section>

    <section class="content">
      <article class="panel table-panel">
        <div class="section-head">
          <h2>Tag aktif saat ini</h2><span class="spacer"></span>
          <input id="search" class="search" placeholder="Cari ID / serial / reader…">
          <button id="clear" class="secondary">Bersihkan</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Tag ID</th><th>Serial</th><th>Status</th><th>RSSI</th><th>Best</th><th>Battery</th><th>Count</th><th>Last seen</th><th>Reader</th></tr></thead>
            <tbody id="tagRows"><tr><td class="empty" colspan="9">Tidak ada tag aktif yang terbaca</td></tr></tbody>
          </table>
        </div>
      </article>
      <article class="panel log-panel">
        <div class="section-head"><h2>Event log</h2><span class="spacer"></span><span id="processState" class="subtitle">STOPPED</span></div>
        <div id="console" class="console"></div>
      </article>
    </section>
  </main>
  <div id="toast" class="toast"></div>

  <script>
    const $ = (id) => document.getElementById(id);
    const fields = ["mode", "reader", "address", "port", "activeTimeout", "maximize", "connectTimeout", "reconnectDelay", "configTimeout"];
    let readers = [];
    let listenHost = "0.0.0.0";
    let defaultPort = "5000";
    let clientAddress = "";
    let clientPort = "5000";
    let previousMode = "client";
    let latestTags = [];
    let lastLogSeq = 0;
    let requestBusy = false;
    let mutationBusy = false;
    let stateEpoch = 0;
    let loadedEnvMtime = null;
    let toastTimer;

    function toast(message, error = false) {
      const el = $("toast");
      el.textContent = message;
      el.className = "toast show" + (error ? " error" : "");
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => el.className = "toast", 3200);
    }

    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: {"Content-Type": "application/json"},
        ...options,
      });
      let body = null;
      try { body = await response.json(); } catch (_) { body = {}; }
      if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
      return body;
    }

    async function loadEnv(notify = true) {
      try {
        const config = await api("/api/config");
        readers = config.readers || [];
        loadedEnvMtime = config.env_mtime_ns;
        listenHost = config.listen_host || "0.0.0.0";
        defaultPort = config.default_port || "5000";
        const configuredMode = ["client", "server"].includes(config.mode) ? config.mode : "client";
        $("mode").value = configuredMode;
        $("reader").replaceChildren();
        readers.forEach((reader, index) => {
          const option = document.createElement("option");
          option.value = String(index);
          option.textContent = `${reader.name} • ${reader.host}:${reader.port} (${reader.source})`;
          $("reader").append(option);
        });
        if (readers.length) {
          $("reader").value = String(config.selected_reader_index || 0);
          const selected = readers[Number($("reader").value)];
          clientAddress = selected.host;
          clientPort = selected.port;
        } else {
          const option = document.createElement("option");
          option.textContent = "Tidak ada reader aktif di .env";
          option.value = "";
          $("reader").append(option);
          clientAddress = "";
          clientPort = defaultPort;
        }
        if (configuredMode === "server") {
          $("address").value = listenHost;
          $("port").value = defaultPort;
        } else {
          $("address").value = clientAddress;
          $("port").value = clientPort;
        }
        previousMode = configuredMode;
        $("activeTimeout").value = config.active_timeout || "5";
        $("envPath").textContent = `.env: ${config.env_path}`;
        applyMode();
        if (notify) toast("Konfigurasi .env terbaru sudah dimuat");
      } catch (error) {
        toast(`Gagal membaca .env: ${error.message}`, true);
      }
    }

    function applyReader() {
      const reader = readers[Number($("reader").value)];
      if (!reader) return;
      clientAddress = reader.host;
      clientPort = reader.port;
      if ($("mode").value === "client") {
        $("address").value = clientAddress;
        $("port").value = clientPort;
      }
    }

    function applyMode() {
      const server = $("mode").value === "server";
      if (server && previousMode === "client") {
        clientAddress = $("address").value;
        clientPort = $("port").value;
      }
      $("addressLabel").textContent = server ? "Bind address" : "IP reader";
      $("reader").disabled = !readers.length;
      if (server) {
        $("address").value = listenHost;
        $("port").value = defaultPort;
      } else {
        $("address").value = clientAddress;
        $("port").value = clientPort;
      }
      previousMode = $("mode").value;
    }

    function scanPayload() {
      const payload = {
        mode: $("mode").value,
        address: $("address").value.trim(),
        port: Number($("port").value),
        active_timeout: Number($("activeTimeout").value),
        connect_timeout: Number($("connectTimeout").value),
        reconnect_delay: Number($("reconnectDelay").value),
        config_timeout: Number($("configTimeout").value),
        maximize_range: $("maximize").checked,
        allowed_reader_ip: $("mode").value === "server" ? clientAddress.trim() : null,
      };
      if (!payload.address) throw new Error("IP/bind address masih kosong");
      if (payload.mode === "server" && !payload.allowed_reader_ip) throw new Error("Mode server memerlukan IP reader aktif di .env");
      for (const [key, value] of Object.entries(payload)) {
        if (typeof value === "number" && (!Number.isFinite(value) || value <= 0)) throw new Error(`${key} tidak valid`);
      }
      if (!Number.isInteger(payload.port) || payload.port > 65535) throw new Error("Port harus 1–65535");
      return payload;
    }

    async function startScan() {
      if (mutationBusy) return;
      mutationBusy = true;
      stateEpoch += 1;
      requestBusy = false;
      try {
        const latestConfig = await api("/api/config");
        if (latestConfig.env_mtime_ns !== loadedEnvMtime) {
          await loadEnv(false);
          toast("Perubahan .env terdeteksi dan diterapkan sebelum Start");
        }
        const payload = scanPayload();
        await api("/api/start", {method: "POST", body: JSON.stringify(payload)});
        toast(`Listener dimulai ke ${payload.address}:${payload.port}`);
      } catch (error) { toast(`Tidak dapat mulai: ${error.message}`, true); }
      finally { mutationBusy = false; }
      await refreshState(true);
    }

    async function stopScan() {
      if (mutationBusy) return;
      mutationBusy = true;
      stateEpoch += 1;
      requestBusy = false;
      try {
        await api("/api/stop", {method: "POST"});
        toast("Permintaan Stop dikirim; menunggu cleanup reader");
      } catch (error) { toast(`Gagal Stop: ${error.message}`, true); }
      finally { mutationBusy = false; }
      await refreshState(true);
    }

    async function clearDashboard() {
      if (!confirm("Bersihkan daftar tag aktif dan log dashboard? Reader/scanning tidak diubah.")) return;
      mutationBusy = true;
      stateEpoch += 1;
      requestBusy = false;
      let cleared = false;
      try {
        await api("/api/clear", {method: "POST"});
        lastLogSeq = 0;
        $("console").replaceChildren();
        cleared = true;
      } catch (error) { toast(error.message, true); }
      finally { mutationBusy = false; }
      if (cleared) await refreshState(true);
    }

    function setRunning(running, processState) {
      $("start").disabled = running;
      $("stop").disabled = !running || processState === "STOPPING";
      fields.forEach((id) => {
        $(id).disabled = running;
      });
      $("reader").disabled = running || !readers.length;
      $("reloadEnv").disabled = running;
      $("processState").textContent = processState;
    }

    function renderPill(state) {
      const pill = $("connectionPill");
      pill.textContent = state;
      const good = ["SCANNING", "TERHUBUNG"];
      const bad = ["TERPUTUS", "GAGAL MEMULAI", "FORCED STOP"];
      const waiting = ["MEMULAI", "MENGHUBUNGKAN", "MENUNGGU READER", "MENCOBA ULANG", "MENGHENTIKAN", "PERINTAH SCAN TERKIRIM"];
      pill.className = "pill" + (good.includes(state) ? " good" : bad.includes(state) || state.startsWith("PROSES BERHENTI") ? " bad" : waiting.includes(state) ? " wait" : "");
    }

    function renderTags(tags) {
      latestTags = tags;
      const term = $("search").value.trim().toLowerCase();
      const filtered = tags.filter((tag) => !term || [tag.id, tag.serial, tag.reader, tag.state].some((value) => String(value).toLowerCase().includes(term)));
      const tbody = $("tagRows");
      tbody.replaceChildren();
      if (!filtered.length) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 9;
        cell.className = "empty";
        cell.textContent = tags.length ? "Tidak ada hasil yang cocok" : "Tidak ada tag aktif yang terbaca";
        row.append(cell); tbody.append(row); return;
      }
      filtered.forEach((tag) => {
        const row = document.createElement("tr");
        const values = [tag.id, tag.serial, tag.state, `${tag.rssi} dBm`, `${tag.best_rssi} dBm`, tag.battery, tag.count, tag.last_seen, tag.reader];
        values.forEach((value, index) => {
          const cell = document.createElement("td");
          if (index === 0) cell.className = "tag-id";
          if (index === 2) {
            const badge = document.createElement("span");
            badge.className = `state ${tag.state}`;
            badge.textContent = tag.state;
            cell.append(badge);
          } else cell.textContent = value;
          row.append(cell);
        });
        tbody.append(row);
      });
    }

    function appendLogs(logs, reset) {
      const consoleEl = $("console");
      if (reset) consoleEl.replaceChildren();
      const nearBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 80;
      logs.forEach((entry) => {
        const line = document.createElement("div");
        line.className = `log-line ${entry.kind}`;
        line.textContent = entry.line;
        consoleEl.append(line);
      });
      while (consoleEl.children.length > 1200) consoleEl.firstChild.remove();
      if (nearBottom) consoleEl.scrollTop = consoleEl.scrollHeight;
    }

    async function refreshState(force = false) {
      if ((requestBusy || mutationBusy) && !force) return;
      const epoch = stateEpoch;
      requestBusy = true;
      try {
        const repeats = $("showRepeats").checked;
        const state = await api(`/api/state?logs_after=${lastLogSeq}&show_repeats=${repeats}`);
        if (epoch !== stateEpoch) return;
        $("unique").textContent = state.stats.unique;
        $("unique").title = state.stats.capacity_dropped
          ? `${state.stats.capacity_dropped} frame tidak dimasukkan karena batas kapasitas pernah tercapai`
          : "";
        $("removed").textContent = state.stats.removed;
        $("reads").textContent = state.stats.reads;
        $("rf").textContent = state.rf_state;
        $("command").textContent = state.command.length ? state.command.join(" ") : "Listener belum dijalankan";
        renderPill(state.connection_state);
        renderTags(state.tags);
        appendLogs(state.logs, state.logs_reset);
        lastLogSeq = state.latest_log_seq;
        setRunning(state.running, state.process_state);
      } catch (error) {
        if (epoch === stateEpoch) renderPill("WEB API TERPUTUS");
      } finally {
        if (epoch === stateEpoch) requestBusy = false;
      }
    }

    $("mode").addEventListener("change", applyMode);
    $("reader").addEventListener("change", applyReader);
    $("address").addEventListener("input", () => {
      if ($("mode").value === "client") clientAddress = $("address").value;
    });
    $("port").addEventListener("input", () => {
      if ($("mode").value === "client") clientPort = $("port").value;
    });
    $("reloadEnv").addEventListener("click", () => loadEnv(true));
    $("start").addEventListener("click", startScan);
    $("stop").addEventListener("click", stopScan);
    $("clear").addEventListener("click", clearDashboard);
    $("search").addEventListener("input", () => renderTags(latestTags));
    $("showRepeats").addEventListener("change", () => {
      stateEpoch += 1;
      requestBusy = false;
      lastLogSeq = 0;
      $("console").replaceChildren();
      refreshState(true);
    });

    loadEnv(false).then(refreshState);
    setInterval(refreshState, 500);
  </script>
</body>
</html>
"""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Dashboard web untuk listener RFID SANRAY F2413"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Alamat bind web")
    parser.add_argument("--port", type=int, default=8010, help="Port dashboard web")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Jangan buka browser otomatis",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port harus berada pada rentang 1..65535")
    if args.host not in {"127.0.0.1", "localhost"}:
        parser.error(
            "--host dashboard harus 127.0.0.1 atau localhost "
            "karena endpoint kontrol reader tidak memakai login"
        )
    return args


def main(argv=None):
    args = parse_args(argv)
    url = f"http://{args.host}:{args.port}"
    print(f"Dashboard RFID: {url}", flush=True)
    print("Tekan Ctrl+C untuk menghentikan dashboard.", flush=True)
    if not args.no_browser:
        timer = threading.Timer(0.9, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
