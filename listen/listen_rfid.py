import argparse
import math
import os
import signal
import socket
import time
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
ENV_FILE_VALUES = {}
DOTENV_AVAILABLE = False
 
try:
    from dotenv import dotenv_values, load_dotenv

    DOTENV_AVAILABLE = True
    ENV_FILE_VALUES = {
        key: str(value)
        for key, value in dotenv_values(ENV_PATH).items()
        if value is not None
    }
    # Untuk tool operator lokal ini, .env proyek adalah konfigurasi utama.
    # Argumen CLI tetap dapat mengalahkannya saat parse_args selesai.
    load_dotenv(dotenv_path=ENV_PATH, override=True)
except ImportError:
    pass


FRAME_HEAD = 0xBB
FRAME_END = b"\x0D\x0A"
ADDRESS_PREFIX = b"\x7E\x7E"


def pack_command(command, payload=b""):
    """Buat frame SANRAY dengan checksum penjumlahan byte rendah."""
    body = bytes([command, len(payload)]) + payload
    checksum = sum(body) & 0xFF
    return bytes([FRAME_HEAD]) + body + bytes([checksum]) + FRAME_END


START_COMMAND = pack_command(0x17, b"\x00\x00")
STOP_COMMAND = pack_command(0x18)
QUERY_MODULE_COMMAND = pack_command(0xFA, b"\x40")

MAX_READER_TX_POWER = 7
MIN_RX_ATTENUATION = 0
TEMPORARY_CONFIG = 0x55
MAX_TRACKED_TAGS = 10000
MAX_SUMMARY_TAGS = 500
STALE_CHECK_INTERVAL = 0.5

# Jika firmware menolak atau mengabaikan konfigurasi maksimum, jangan mencoba
# menulisnya lagi untuk peer yang sama sampai proses listener dimulai ulang.
max_range_blocked_peers = set()
dropped_unknown_tags = 0
next_capacity_report_monotonic = 0.0


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log(message):
    print(f"[{now()}] {message}", flush=True)


def format_hex(data):
    return data.hex(" ").upper()


def frame_is_valid(frame):
    """Validasi struktur, panjang, terminator, dan checksum satu frame."""
    if len(frame) < 6 or frame[0] != FRAME_HEAD:
        return False
    if len(frame) != frame[2] + 6 or frame[-2:] != FRAME_END:
        return False
    return (sum(frame[1:-3]) & 0xFF) == frame[-3]


def address_trailer_is_valid(trailer):
    if len(trailer) != 7 or trailer[:2] != ADDRESS_PREFIX:
        return False
    checksum = 0
    for value in trailer[2:6]:
        checksum ^= value
    return checksum == trailer[6]


def extract_frames(buffer):
    """Ambil frame lengkap BB ... 0D 0A berdasarkan Len, bukan delimiter saja."""
    frames = []

    while buffer:
        # Beberapa firmware menambahkan alamat reader setelah 0D 0A:
        # 7E 7E + address[4] + XOR. Trailer tidak termasuk Len/CRC core frame.
        if buffer.startswith(ADDRESS_PREFIX):
            if len(buffer) < 7:
                return frames, buffer
            trailer = buffer[:7]
            buffer = buffer[7:]
            if not address_trailer_is_valid(trailer):
                log(f"Trailer alamat reader tidak valid: {format_hex(trailer)}")
            continue
        if buffer == ADDRESS_PREFIX[:1]:
            return frames, buffer

        start = buffer.find(bytes([FRAME_HEAD]))
        if start == -1:
            return frames, b""
        if start > 0:
            buffer = buffer[start:]
        if len(buffer) < 3:
            return frames, buffer

        frame_length = buffer[2] + 6
        if len(buffer) < frame_length:
            # Jika head/Len pertama rusak, jangan menunggu sampai 261 byte.
            # Berpindah hanya bila kandidat BB berikutnya sudah membentuk frame
            # lengkap dan checksum-valid; BB di dalam payload parsial tetap aman.
            next_start = buffer.find(bytes([FRAME_HEAD]), 1)
            resynced = False
            while next_start != -1:
                candidate = buffer[next_start:]
                if len(candidate) >= 3:
                    candidate_length = candidate[2] + 6
                    if len(candidate) >= candidate_length and frame_is_valid(
                        candidate[:candidate_length]
                    ):
                        buffer = candidate
                        resynced = True
                        break
                next_start = buffer.find(bytes([FRAME_HEAD]), next_start + 1)
            if resynced:
                continue
            return frames, buffer

        frame = buffer[:frame_length]
        if not frame_is_valid(frame):
            # Head/Len palsu atau data rusak: geser satu byte lalu cari BB lagi.
            buffer = buffer[1:]
            continue

        frames.append(frame)
        buffer = buffer[frame_length:]

    return frames, buffer


def decode_signed_byte(value, invalid=0x80):
    if value == invalid:
        return None
    return value if value < 0x80 else value - 0x100


def decode_bcd(data):
    digits = []
    for value in data:
        high, low = value >> 4, value & 0x0F
        if high > 9 or low > 9:
            return None
        digits.extend((str(high), str(low)))
    return "".join(digits)


def parse_tag(frame):
    """Parse frame laporan tag aktif 2.4G, command F1, Len 0B."""
    if not frame_is_valid(frame):
        return None
    if frame[1] != 0xF1 or frame[2] != 0x0B:
        return None

    raw_id_bytes = frame[4:9]
    raw_id = raw_id_bytes.hex().upper()
    voltage_code = raw_id_bytes[0] >> 4
    tag_type = raw_id_bytes[0] & 0x0F

    # Tag minimal/read-only tipe 5 menyimpan nomor kartu sebagai BCD pada
    # empat byte terakhir. Byte pertama adalah metadata tegangan + tipe.
    serial_bcd = decode_bcd(raw_id_bytes[1:]) if tag_type == 5 else None

    battery_raw = frame[13]
    if tag_type == 5:
        if battery_raw == 0xFF:
            battery_status = "NORMAL"
        elif battery_raw == 0x00:
            battery_status = "LOW"
        else:
            battery_status = f"UNKNOWN(0x{battery_raw:02X})"
        battery_voltage = None
    else:
        battery_status = "MEASURED"
        battery_voltage = round(battery_raw * 0.02, 2)

    tag_tx_power = decode_signed_byte(frame[10])
    return {
        # Pertahankan ID raw 5 byte sebagai key agar kompatibel dengan output
        # listener lama dan tidak collision antara metadata 0x05 vs 0x15.
        "tag_id": raw_id,
        "raw_id": raw_id,
        "serial_bcd": serial_bcd,
        "tag_type": tag_type,
        "voltage_code": voltage_code,
        "temperature_c": decode_signed_byte(frame[3]),
        "rssi_dbm": -frame[9],
        "tag_tx_power_dbm": tag_tx_power,
        # Alias kompatibilitas; ini power milik tag, bukan power receiver.
        "power": tag_tx_power,
        "tamper_count": frame[11],
        "interval_seconds": frame[12],
        "battery_raw": battery_raw,
        "battery_status": battery_status,
        "battery_voltage": battery_voltage,
        "raw": format_hex(frame),
    }


def parse_module_config(frame):
    """Parse respons FB/40 standar atau varian F2413 ber-extension 0xFF."""
    if not frame_is_valid(frame):
        return None
    if frame[1] != 0xFB or frame[2] not in {0x09, 0x0A} or frame[3] != 0x40:
        return None
    # Firmware F2413 yang diuji menambahkan satu byte reserved 0xFF setelah
    # MODE. Field konfigurasi utama tetap berada pada offset yang sama.
    if frame[2] == 0x0A and frame[12] != 0xFF:
        return None

    config = {
        "save": frame[4],
        "version": frame[5],
        "tx_power": frame[6],
        "rx_attenuation": frame[7],
        "work_state": frame[8],
        "channel": frame[9],
        "tag_type": frame[10],
        "mode": frame[11],
        "response_extension": frame[12] if frame[2] == 0x0A else None,
    }
    if config["save"] not in {0x55, 0x66}:
        return None
    if config["version"] not in {0x00, 0x01, 0x02}:
        return None
    if not 0 <= config["tx_power"] <= 7:
        return None
    if not 0 <= config["rx_attenuation"] <= 63:
        return None
    if config["work_state"] not in {0x00, 0x01}:
        return None
    if not 0 <= config["channel"] <= 80:
        return None
    if not 0 <= config["tag_type"] <= 15:
        return None
    if config["mode"] not in {0x00, 0x01}:
        return None
    return config


def module_config_rejection_reason(frame):
    """Jelaskan alasan respons konfigurasi ditolak tanpa melonggarkan guard."""
    if frame is None:
        return "timeout/tidak ada respons"
    if not frame_is_valid(frame):
        return "struktur, panjang, terminator, atau checksum tidak valid"
    if frame[1] != 0xFB:
        return f"command 0x{frame[1]:02X}, seharusnya 0xFB"
    if frame[2] not in {0x09, 0x0A}:
        return f"Len=0x{frame[2]:02X}, seharusnya 0x09/0x0A"
    if frame[3] != 0x40:
        return f"subcommand 0x{frame[3]:02X}, seharusnya 0x40"
    if frame[2] == 0x0A and frame[12] != 0xFF:
        return (
            f"extension F2413=0x{frame[12]:02X}, seharusnya 0xFF"
        )

    fields = {
        "SAVE": frame[4],
        "VER": frame[5],
        "TX": frame[6],
        "RX_ATTEN": frame[7],
        "WORK": frame[8],
        "CHANNEL": frame[9],
        "TAGTYPE": frame[10],
        "MODE": frame[11],
    }
    checks = (
        (fields["SAVE"] in {0x55, 0x66}, "SAVE harus 0x55/0x66"),
        (fields["VER"] in {0x00, 0x01, 0x02}, "VER harus 0..2"),
        (0 <= fields["TX"] <= 7, "TX harus 0..7"),
        (0 <= fields["RX_ATTEN"] <= 63, "RX_ATTEN harus 0..63"),
        (fields["WORK"] in {0x00, 0x01}, "WORK harus 0/1"),
        (0 <= fields["CHANNEL"] <= 80, "CHANNEL harus 0..80"),
        (0 <= fields["TAGTYPE"] <= 15, "TAGTYPE harus 0..15"),
        (fields["MODE"] in {0x00, 0x01}, "MODE harus 0/1"),
    )
    for valid, reason in checks:
        if not valid:
            values = " ".join(
                f"{name}=0x{value:02X}" for name, value in fields.items()
            )
            return f"{reason}; {values}"
    return "respons tampak valid; cek perbedaan parser"


def build_max_range_command(config):
    """Salin konfigurasi reader lalu ubah hanya TX/RX secara volatile."""
    payload = bytes(
        [
            0x00,
            TEMPORARY_CONFIG,
            config["version"],
            MAX_READER_TX_POWER,
            MIN_RX_ATTENUATION,
            config["work_state"],
            config["channel"],
            config["tag_type"],
            config["mode"],
        ]
    )
    return pack_command(0xFA, payload)


def module_configs_match(expected, actual):
    if not actual:
        return False
    return (
        actual["save"] == TEMPORARY_CONFIG
        and actual["version"] == expected["version"]
        and actual["tx_power"] == MAX_READER_TX_POWER
        and actual["rx_attenuation"] == MIN_RX_ATTENUATION
        and actual["work_state"] == expected["work_state"]
        and actual["channel"] == expected["channel"]
        and actual["tag_type"] == expected["tag_type"]
        and actual["mode"] == expected["mode"]
    )


def response_error_code(frame):
    if (
        frame
        and frame_is_valid(frame)
        and frame[1] == 0xFF
        and frame[2] == 0x02
    ):
        return (frame[3] << 8) | frame[4]
    return None


def send_command(sock, command, label):
    try:
        sock.sendall(command)
        log(f"TX {label}: {format_hex(command)}")
        return True
    except OSError as exc:
        log(f"Gagal mengirim {label}: {exc}")
        return False


def wait_for_matching_frame(sock, buffer, queued_frames, predicate, timeout):
    """Tunggu respons sambil mempertahankan frame tag yang datang bersamaan."""
    deadline = time.monotonic() + timeout

    while True:
        frames, buffer = extract_frames(buffer)
        matched = None
        for frame in frames:
            if matched is None and predicate(frame):
                matched = frame
            else:
                queued_frames.append(frame)
        if matched is not None:
            return matched, buffer

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, buffer

        sock.settimeout(min(0.25, remaining))
        try:
            data = sock.recv(65535)
        except socket.timeout:
            continue
        if not data:
            raise ConnectionError("Receiver memutus koneksi saat konfigurasi")
        buffer += data


def request_module_config(sock, buffer, queued_frames, timeout, label):
    if not send_command(sock, QUERY_MODULE_COMMAND, label):
        return None, buffer, None

    response, buffer = wait_for_matching_frame(
        sock,
        buffer,
        queued_frames,
        lambda frame: (
            frame[1] == 0xFF
            or (frame[1] == 0xFB and frame[2] >= 1 and frame[3] == 0x40)
        ),
        timeout,
    )
    return parse_module_config(response) if response else None, buffer, response


def try_maximize_range(sock, buffer, queued_frames, timeout, peer_label):
    """Best-effort TX=7/RX attenuation=0; hanya setelah guard lengkap lolos."""
    if peer_label in max_range_blocked_peers:
        log(f"Mode maximize-range untuk {peer_label} dinonaktifkan sampai restart")
        return buffer

    if not send_command(sock, STOP_COMMAND, "STOP SEBELUM CONFIG"):
        return buffer

    stop_response, buffer = wait_for_matching_frame(
        sock,
        buffer,
        queued_frames,
        lambda frame: frame[1] in {0x98, 0xFF},
        timeout,
    )
    stop_confirmed = bool(
        stop_response
        and stop_response[1] == 0x98
        and stop_response[2] == 0x01
        and stop_response[3] == 0x01
    )
    if not stop_confirmed:
        log(
            "STOP tidak dikonfirmasi; parameter RF tidak diubah. "
            "Pembacaan akan dilanjutkan."
        )
        return buffer

    config, buffer, response = request_module_config(
        sock, buffer, queued_frames, timeout, "QUERY CONFIG 2.4G"
    )
    error_code = response_error_code(response)
    if error_code is not None:
        log(f"Query konfigurasi 2.4G ditolak, error=0x{error_code:04X}")
        max_range_blocked_peers.add(peer_label)
        return buffer
    if not config:
        if response is not None:
            log(f"RX QUERY CONFIG 2.4G RAW: {format_hex(response)}")
        reason = module_config_rejection_reason(response)
        log(
            "Receiver tidak mengembalikan konfigurasi 2.4G yang valid "
            f"({reason}); "
            "parameter RF tidak diubah."
        )
        return buffer

    channel_mhz = 2400 + config["channel"]
    extension = config["response_extension"]
    extension_text = "N/A" if extension is None else f"0x{extension:02X}"
    log(
        "CONFIG AWAL: "
        f"VER={config['version']} TX={config['tx_power']} "
        f"RX_ATTEN={config['rx_attenuation']} CHANNEL={channel_mhz}MHz "
        f"TAGTYPE={config['tag_type']} MODE={config['mode']} "
        f"EXT={extension_text}"
    )

    if config["version"] != 0x02:
        log("Protocol module bukan v2; maximize-range dilewati agar format F1 aman")
        return buffer
    if (
        config["tx_power"] == MAX_READER_TX_POWER
        and config["rx_attenuation"] == MIN_RX_ATTENUATION
    ):
        log("Parameter runtime sudah TX=7 dan RX attenuation=0; SET dilewati")
        return buffer
    if config["work_state"] != 0x01:
        log(
            "STOP mendapat ACK tetapi WORK_STATE masih 0 (membaca); SET RF "
            "dilewati agar konfigurasi modul tidak abnormal."
        )
        return buffer

    set_command = build_max_range_command(config)
    if not send_command(sock, set_command, "SET MAX RANGE (VOLATILE)"):
        return buffer

    set_response, buffer = wait_for_matching_frame(
        sock,
        buffer,
        queued_frames,
        lambda frame: (
            frame[1] == 0xFF
            or (frame[1] == 0xFB and frame[2] >= 1 and frame[3] == 0x00)
        ),
        timeout,
    )
    set_acknowledged = bool(
        set_response
        and set_response[1] == 0xFB
        and set_response[2] == 0x02
        and set_response[3] == 0x00
        and set_response[4] == 0x01
    )
    if not set_acknowledged:
        error_code = response_error_code(set_response)
        if error_code is not None:
            log(f"SET runtime ditolak, error=0x{error_code:04X}")
            max_range_blocked_peers.add(peer_label)
        else:
            log("ACK SET tidak diterima; melakukan query ulang untuk verifikasi")

    verified, buffer, verify_response = request_module_config(
        sock, buffer, queued_frames, timeout, "VERIFY CONFIG 2.4G"
    )
    if module_configs_match(config, verified):
        log(
            "Parameter runtime terverifikasi: TX=7, RX attenuation=0, "
            "SAVE=0x55. Menurut SDK, flag 0x55 meminta parameter tidak "
            "ditulis ke flash, tetapi konfigurasi runtime dapat aktif sampai "
            "reader reboot/dikonfigurasi ulang dan tidak otomatis dipulihkan "
            "saat script berhenti. Dampak RF F2413 tetap perlu diuji."
        )
    elif verify_response is None:
        log(
            "Perubahan runtime tidak terkonfirmasi; status parameter mungkin "
            "tidak berubah. Percobaan SET berikutnya diblokir sampai listener "
            "direstart."
        )
        max_range_blocked_peers.add(peer_label)
    else:
        verify_error = response_error_code(verify_response)
        if verify_error is not None:
            log(f"Query verifikasi ditolak, error=0x{verify_error:04X}")
        elif verified:
            log(
                "Konfigurasi hasil query ulang tidak cocok: "
                f"SAVE=0x{verified['save']:02X} VER={verified['version']} "
                f"TX={verified['tx_power']} "
                f"RX_ATTEN={verified['rx_attenuation']} "
                f"WORK={verified['work_state']} "
                f"CHANNEL={2400 + verified['channel']}MHz "
                f"TAGTYPE={verified['tag_type']} MODE={verified['mode']}"
            )
        else:
            log(f"RX VERIFY CONFIG 2.4G RAW: {format_hex(verify_response)}")
            log(
                "Respons query verifikasi tidak memiliki format yang valid "
                f"({module_config_rejection_reason(verify_response)})"
            )
        log("Percobaan SET berikutnya diblokir sampai listener direstart")
        max_range_blocked_peers.add(peer_label)
    return buffer


def configure_socket(sock):
    """Kurangi risiko frame TCP tertunda/hilang saat tag datang bertubi-tubi."""
    for level, option, value in (
        (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
        (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        (socket.SOL_SOCKET, socket.SO_RCVBUF, 262144),
    ):
        try:
            sock.setsockopt(level, option, value)
        except OSError:
            pass


def peer_name(sock):
    try:
        return str(sock.getpeername()[0])
    except (OSError, IndexError, TypeError):
        return "unknown-reader"


def prepare_reader(sock, maximize_range, config_timeout):
    """Siapkan reader lalu START; kegagalan protokol tetap memakai config lama."""
    buffer = b""
    queued_frames = []
    peer_label = peer_name(sock)
    if maximize_range:
        buffer = try_maximize_range(
            sock, buffer, queued_frames, config_timeout, peer_label
        )
    if not send_command(sock, START_COMMAND, "START CONTINUOUS"):
        raise ConnectionError("Reader tidak dapat memulai continuous scan")
    return buffer, queued_frames, peer_label


def update_detected_tag(
    detected_tags, tag, reader_label, seen_monotonic=None, seen_at=None
):
    """Simpan hanya tag aktif secara bounded; kembalikan new/repeat/capacity."""
    if seen_monotonic is None:
        seen_monotonic = time.monotonic()
    if seen_at is None:
        seen_at = now()
    tag_id = tag["tag_id"]

    if tag_id not in detected_tags:
        if len(detected_tags) >= MAX_TRACKED_TAGS:
            return "capacity", None
        detected_tags[tag_id] = {
            **tag,
            "reader": reader_label,
            "count": 1,
            "active": True,
            "state": "ACTIVE",
            "first_seen": seen_at,
            "last_seen": seen_at,
            "last_seen_monotonic": seen_monotonic,
            "best_rssi_dbm": tag["rssi_dbm"],
            "worst_rssi_dbm": tag["rssi_dbm"],
        }
        return "new", detected_tags[tag_id]

    stored = detected_tags[tag_id]
    count = stored["count"] + 1
    first_seen = stored["first_seen"]
    best_rssi = max(stored["best_rssi_dbm"], tag["rssi_dbm"])
    worst_rssi = min(stored["worst_rssi_dbm"], tag["rssi_dbm"])
    stored.update(tag)
    stored.update(
        {
            "reader": reader_label,
            "count": count,
            "active": True,
            "state": "ACTIVE",
            "first_seen": first_seen,
            "last_seen": seen_at,
            "last_seen_monotonic": seen_monotonic,
            "best_rssi_dbm": best_rssi,
            "worst_rssi_dbm": worst_rssi,
        }
    )
    return "repeat", stored


def stale_timeout_for(tag, base_timeout):
    # Demo vendor menyebut field interval masih reserved/tidak selalu bermakna.
    # Gunakan batas eksplisit agar nilai liar tidak membuat tag terlihat ACTIVE
    # sampai beberapa menit. Sesuaikan lewat --active-timeout bila diperlukan.
    del tag
    return base_timeout


def mark_stale_tags(detected_tags, active_timeout, current_monotonic=None):
    """Hapus dan kembalikan tag yang tidak lagi terbaca."""
    if current_monotonic is None:
        current_monotonic = time.monotonic()
    stale = []
    for tag_id, tag in list(detected_tags.items()):
        age = current_monotonic - tag["last_seen_monotonic"]
        if age > stale_timeout_for(tag, active_timeout):
            detected_tags.pop(tag_id, None)
            stale.append((tag, age))
    return stale


def mark_reader_disconnected(detected_tags, reader_label):
    """Hapus seluruh tag milik reader yang koneksinya berakhir."""
    disconnected = []
    for tag_id, tag in list(detected_tags.items()):
        if tag["reader"] == reader_label:
            detected_tags.pop(tag_id, None)
            disconnected.append(tag)
    return disconnected


def format_optional(value, suffix=""):
    return "N/A" if value is None else f"{value}{suffix}"


def process_tag_frame(
    frame, detected_tags, reader_label, show_repeats, active_timeout
):
    global dropped_unknown_tags, next_capacity_report_monotonic

    tag = parse_tag(frame)
    if not tag:
        return

    seen_monotonic = time.monotonic()
    seen_at = now()
    event, stored = update_detected_tag(
        detected_tags, tag, reader_label, seen_monotonic, seen_at
    )
    if event == "capacity":
        # Capacity adalah jumlah tag aktif bersamaan. Bebaskan entry yang
        # sudah kedaluwarsa dengan timestamp yang sama, lalu coba sekali lagi.
        removed = log_stale_tags(
            detected_tags, active_timeout, seen_monotonic
        )
        if removed:
            event, stored = update_detected_tag(
                detected_tags, tag, reader_label, seen_monotonic, seen_at
            )
    if event == "capacity":
        dropped_unknown_tags += 1
        is_power_of_two = (
            dropped_unknown_tags & (dropped_unknown_tags - 1)
        ) == 0
        # Pangkat dua + maksimal dua kali/detik menjaga agregat tetap terukur
        # pada burst singkat tanpa mencetak satu baris untuk setiap frame.
        if is_power_of_two or seen_monotonic >= next_capacity_report_monotonic:
            log(
                f"[CAPACITY] Batas {MAX_TRACKED_TAGS} tag aktif tercapai; "
                f"DROPPED_TOTAL={dropped_unknown_tags}"
            )
            next_capacity_report_monotonic = (
                seen_monotonic + STALE_CHECK_INTERVAL
            )
        return
    details = (
        f"ID={stored['tag_id']} RAW_ID={stored['raw_id']} "
        f"SERIAL={format_optional(stored['serial_bcd'])} "
        f"READER={stored['reader']} RSSI={stored['rssi_dbm']}dBm "
        f"TAG_TX={format_optional(stored['tag_tx_power_dbm'], 'dBm')} "
        f"BAT={stored['battery_status']}"
    )
    if event == "new":
        log(f"[TAG #{len(detected_tags)}] {details}")
    elif show_repeats:
        log(f"[REPEAT x{stored['count']}] {details}")


def log_stale_tags(
    detected_tags, active_timeout, current_monotonic=None
):
    stale = mark_stale_tags(
        detected_tags, active_timeout, current_monotonic
    )
    for tag, age in stale:
        log(
            f"[REMOVE] ID={tag['tag_id']} REASON=TIMEOUT "
            f"tidak menerima frame selama {age:.3f}s; "
            "dihapus dari daftar aktif"
        )
    return len(stale)


def listen_connection(sock, detected_tags, args):
    """Baca satu koneksi receiver terus-menerus sampai terputus."""
    configure_socket(sock)
    reader_label = peer_name(sock)
    try:
        buffer, queued_frames, reader_label = prepare_reader(
            sock, args.maximize_range, args.config_timeout
        )
        sock.settimeout(args.socket_timeout)
        next_stale_check = time.monotonic()

        for frame in queued_frames:
            process_tag_frame(
                frame,
                detected_tags,
                reader_label,
                args.show_repeats,
                args.active_timeout,
            )

        while True:
            frames, buffer = extract_frames(buffer)
            for frame in frames:
                process_tag_frame(
                    frame,
                    detected_tags,
                    reader_label,
                    args.show_repeats,
                    args.active_timeout,
                )
            current_monotonic = time.monotonic()
            if current_monotonic >= next_stale_check:
                log_stale_tags(
                    detected_tags, args.active_timeout, current_monotonic
                )
                next_stale_check = current_monotonic + STALE_CHECK_INTERVAL

            try:
                data = sock.recv(65535)
            except socket.timeout:
                continue
            if not data:
                log("Receiver memutus koneksi")
                return
            buffer += data
    finally:
        send_command(sock, STOP_COMMAND, "STOP")
        disconnected = mark_reader_disconnected(detected_tags, reader_label)
        if disconnected:
            log(
                f"[DISCONNECTED] READER={reader_label}, "
                f"{len(disconnected)} tag dihapus dari daftar aktif"
            )


def run_server(args, detected_tags):
    """Menunggu receiver melakukan koneksi ke komputer ini."""
    try:
        allowed_reader_ip = socket.gethostbyname(args.allowed_reader_ip)
    except socket.gaierror as exc:
        raise OSError(
            f"Allowed reader IP tidak dapat di-resolve: {args.allowed_reader_ip}"
        ) from exc

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.host, args.port))
        server.listen(1)
        server.settimeout(1)

        log(f"Listening di {args.host}:{args.port}")
        log(f"Mode server hanya menerima receiver dari {allowed_reader_ip}")
        log("Menunggu RFID receiver terhubung...")

        while True:
            try:
                sock, address = server.accept()
            except socket.timeout:
                log_stale_tags(detected_tags, args.active_timeout)
                continue

            if address[0] != allowed_reader_ip:
                log(
                    f"Koneksi dari {address[0]}:{address[1]} ditolak; "
                    f"allowed reader={allowed_reader_ip}"
                )
                sock.close()
                continue

            with sock:
                log(f"Receiver terhubung dari {address[0]}:{address[1]}")
                try:
                    listen_connection(sock, detected_tags, args)
                except (OSError, ConnectionError) as exc:
                    log(f"Koneksi receiver berakhir: {exc}")
                log("Menunggu receiver terhubung kembali...")


def run_client(args, detected_tags):
    """Terhubung ke reader dan otomatis mencoba kembali bila koneksi putus."""
    while True:
        log(f"Menghubungkan ke receiver {args.reader_ip}:{args.port}")
        try:
            with socket.create_connection(
                (args.reader_ip, args.port), timeout=args.connect_timeout
            ) as sock:
                log(f"Terhubung ke receiver {args.reader_ip}:{args.port}")
                listen_connection(sock, detected_tags, args)
        except (OSError, ConnectionError) as exc:
            log(f"Koneksi reader terputus/gagal: {exc}")

        log_stale_tags(detected_tags, args.active_timeout)
        log(f"Mencoba terhubung kembali dalam {args.reconnect_delay:g} detik...")
        time.sleep(args.reconnect_delay)


def env_value(name, default=None):
    """Ambil nilai .env proyek lebih dulu, lalu environment proses."""
    value = ENV_FILE_VALUES.get(name)
    if value is None:
        value = os.getenv(name, default)
    return value


def resolve_reader_defaults():
    """Pilih satu reader bernomor tanpa mengharuskan urutan mulai dari 1."""
    default_port = env_value("RFID_PORT", "5000")
    env_numbered = {
        key: value
        for key, value in ENV_FILE_VALUES.items()
        if key.startswith("RFID_READER_") and key.endswith("_HOST")
    }
    source = env_numbered if env_numbered else os.environ
    readers = []
    for key, value in source.items():
        if not key.startswith("RFID_READER_") or not key.endswith("_HOST"):
            continue
        number_text = key[len("RFID_READER_") : -len("_HOST")]
        host = str(value).strip()
        if number_text.isdigit() and host:
            readers.append((int(number_text), host, key))
    readers.sort()

    selected_id = str(env_value("RFID_READER_ID", "")).strip()
    selected = next(
        (reader for reader in readers if str(reader[0]) == selected_id),
        readers[0] if readers else None,
    )
    if selected:
        reader_id, host, host_source = selected
        reader_port_name = f"RFID_READER_{reader_id}_PORT"
        reader_port = env_value(reader_port_name)
        if reader_port:
            return host, reader_port, host_source, reader_port_name
        return host, default_port, host_source, "RFID_PORT"

    legacy_host = str(env_value("RFID_READER_HOST", "")).strip()
    if legacy_host:
        return legacy_host, default_port, "RFID_READER_HOST", "RFID_PORT"
    return "172.15.5.222", default_port, "fallback bawaan", "RFID_PORT"


def parse_args():
    default_reader_ip, default_port, ip_source, port_source = (
        resolve_reader_defaults()
    )
    default_mode = str(env_value("RFID_MODE", "client")).lower()
    if default_mode not in {"server", "client"}:
        default_mode = "client"

    parser = argparse.ArgumentParser(
        description="Listener continuous untuk active RFID SANRAY F2413 2.45GHz"
    )
    parser.add_argument(
        "--mode",
        choices=["server", "client"],
        default=default_mode,
        help="server: tunggu receiver terhubung; client: hubungkan ke IP receiver",
    )
    parser.add_argument(
        "--host",
        default=env_value("RFID_LISTEN_HOST", "0.0.0.0"),
        help="Alamat bind untuk mode server",
    )
    parser.add_argument("--reader-ip")
    parser.add_argument(
        "--allowed-reader-ip",
        help="IP receiver yang diizinkan terhubung pada mode server",
    )
    parser.add_argument("--port", type=int)
    parser.add_argument("--connect-timeout", type=float, default=5.0)
    parser.add_argument("--socket-timeout", type=float, default=0.5)
    parser.add_argument("--reconnect-delay", type=float, default=3.0)
    parser.add_argument(
        "--active-timeout",
        type=float,
        default=env_value("RFID_TAG_ACTIVE_TIMEOUT", "5"),
        help="Batas tanpa frame sebelum tag dihapus dari daftar aktif",
    )
    parser.add_argument(
        "--show-repeats",
        action="store_true",
        help="Tampilkan setiap pembacaan berulang beserta RSSI yang benar",
    )
    range_group = parser.add_mutually_exclusive_group()
    range_group.add_argument(
        "--maximize-range",
        dest="maximize_range",
        action="store_true",
        help=(
            "Aktifkan konfigurasi runtime TX=7/RX attenuation=0 (default)"
        ),
    )
    range_group.add_argument(
        "--no-maximize-range",
        dest="maximize_range",
        action="store_false",
        help="Lewati konfigurasi RF maksimum dan gunakan setting reader saat ini",
    )
    parser.set_defaults(maximize_range=True)
    parser.add_argument(
        "--config-timeout",
        type=float,
        default=2.0,
        help="Timeout masing-masing respons konfigurasi 2.4G",
    )
    args = parser.parse_args()
    if args.reader_ip is None:
        args.reader_ip = default_reader_ip
        args.reader_ip_source = ip_source
    else:
        args.reader_ip_source = "argumen --reader-ip"
    if args.allowed_reader_ip is None:
        args.allowed_reader_ip = default_reader_ip
    if args.port is None:
        try:
            args.port = int(default_port)
        except (TypeError, ValueError):
            parser.error(f"{port_source} harus berupa angka bulat")
        args.port_source = port_source
    else:
        args.port_source = "argumen --port"
    positive_numbers = {
        "--connect-timeout": args.connect_timeout,
        "--socket-timeout": args.socket_timeout,
        "--reconnect-delay": args.reconnect_delay,
        "--active-timeout": args.active_timeout,
        "--config-timeout": args.config_timeout,
    }
    for option, value in positive_numbers.items():
        if not math.isfinite(value) or value <= 0:
            parser.error(f"{option} harus berupa angka finite dan lebih besar dari 0")
    if not 1 <= args.port <= 65535:
        parser.error("--port harus berada pada rentang 1..65535")
    return args


def print_summary(detected_tags, active_timeout):
    mark_stale_tags(detected_tags, active_timeout)
    log(f"Tag aktif saat shutdown: {len(detected_tags)}")
    if dropped_unknown_tags:
        log(
            f"[CAPACITY FINAL] Batas {MAX_TRACKED_TAGS} tag aktif; "
            f"DROPPED_TOTAL={dropped_unknown_tags}"
        )
    tag_ids = sorted(detected_tags)
    for number, tag_id in enumerate(tag_ids[:MAX_SUMMARY_TAGS], start=1):
        tag = detected_tags[tag_id]
        log(
            f"  {number}. {tag_id} | ACTIVE | READER={tag['reader']} "
            f"| RSSI={tag['rssi_dbm']}dBm "
            f"(best={tag['best_rssi_dbm']}, worst={tag['worst_rssi_dbm']}) "
            f"| terbaca={tag['count']}x | last={tag['last_seen']}"
        )
    omitted = len(tag_ids) - MAX_SUMMARY_TAGS
    if omitted > 0:
        log(
            f"  ... {omitted} tag lain tidak dicetak agar shutdown tetap "
            "responsif"
        )


def main():
    args = parse_args()
    detected_tags = {}
    log("Listener RFID F2413 aktif (continuous scan)")
    if ENV_PATH.is_file() and not DOTENV_AVAILABLE:
        log(
            f"PERINGATAN: {ENV_PATH} ditemukan tetapi python-dotenv tidak "
            "tersedia; install python-dotenv atau gunakan argumen CLI"
        )
    if args.mode == "client":
        log(
            f"Target reader {args.reader_ip}:{args.port} | "
            f"IP dari {args.reader_ip_source} | port dari {args.port_source}"
        )
    else:
        log(f"Mode server bind ke {args.host}:{args.port}")
    if args.maximize_range:
        log(
            "Mode --maximize-range aktif: akan mencoba konfigurasi "
            "runtime/volatile setelah seluruh guard lolos"
        )

    try:
        if args.mode == "server":
            run_server(args, detected_tags)
        else:
            run_client(args, detected_tags)
    except KeyboardInterrupt:
        log("Dihentikan dengan Ctrl+C")
    except OSError as exc:
        log(f"Socket error: {exc}")
    finally:
        print_summary(detected_tags, args.active_timeout)


if __name__ == "__main__":

    def handle_termination(signum, frame):
        del signum, frame
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handle_termination)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, handle_termination)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handle_termination)
    main()