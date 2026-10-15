import argparse
import os
import socket
import time

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


GET_WORK_MODE = 0x34
GET_WORK_MODE_RESPONSE = 0xC4
SET_WORK_MODE = 0x37
SET_WORK_MODE_RESPONSE = 0xC7
WORK_MODE_SIZE = 20
READ_BUZZER_INDEX = 6
WRITE_BUZZER_INDEX = 7


def pack_command(command, payload=b""):
    body = bytes([command, len(payload)]) + payload
    checksum = sum(body) & 0xFF
    return b"\xBB" + body + bytes([checksum]) + b"\x0D\x0A"


def connect_reader(mode, host, port, listen_host, timeout):
    if mode == "server":
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((listen_host, port))
        server.listen(1)
        server.settimeout(timeout)
        try:
            sock, _ = server.accept()
        except socket.timeout:
            server.close()
            raise RuntimeError(
                f"Tidak ada reader yang terhubung ke {listen_host}:{port} "
                f"dalam {timeout:g} detik"
            )
        server.close()
        return sock
    return socket.create_connection((host, port), timeout=timeout)


def send_and_wait(sock, command, expected_response, timeout):
    deadline = time.monotonic() + timeout
    buffer = bytearray()

    sock.settimeout(0.4)
    sock.sendall(command)

    while time.monotonic() < deadline:
        try:
            data = sock.recv(65535)
        except socket.timeout:
            continue

        if not data:
            break
        buffer.extend(data)

        while True:
            start = buffer.find(b"\xBB")
            if start < 0:
                buffer.clear()
                break
            if start:
                del buffer[:start]
            if len(buffer) < 3:
                break

            frame_size = buffer[2] + 6
            if len(buffer) < frame_size:
                break

            frame = bytes(buffer[:frame_size])
            del buffer[:frame_size]
            if not frame.endswith(b"\x0D\x0A"):
                del buffer[:1]
                continue
            if frame[1] == expected_response:
                return frame

    raise RuntimeError(
        f"Reader tidak memberikan respons 0x{expected_response:02X} "
        f"dalam {timeout:g} detik"
    )


def read_work_mode(sock, timeout):
    response = send_and_wait(
        sock,
        pack_command(GET_WORK_MODE),
        GET_WORK_MODE_RESPONSE,
        timeout,
    )
    if len(response) < 3 + WORK_MODE_SIZE + 3:
        raise RuntimeError("Respons work mode reader terlalu pendek")
    return bytearray(response[3 : 3 + WORK_MODE_SIZE])


def buzzer_state(work_mode):
    return (
        bool(work_mode[READ_BUZZER_INDEX] & 0x01),
        bool(work_mode[WRITE_BUZZER_INDEX] & 0x01),
        not bool(work_mode[WRITE_BUZZER_INDEX] & 0x80),
    )


def buzzer_is_on(work_mode):
    return any(buzzer_state(work_mode))


def format_buzzer_state(state):
    return ", ".join(
        f"{name}: {'ON' if enabled else 'OFF'}"
        for name, enabled in zip(("Baca", "Tulis", "Status"), state)
    )


def set_buzzer(sock, timeout, enabled):
    work_mode = read_work_mode(sock, timeout)
    before = buzzer_state(work_mode)

    # Bit 0 mengatur buzzer. Bit 7 (sinkronisasi GPIO0) dipertahankan.
    work_mode[READ_BUZZER_INDEX] = (
        (work_mode[READ_BUZZER_INDEX] & 0xFE) | int(enabled)
    )
    work_mode[WRITE_BUZZER_INDEX] = (
        (work_mode[WRITE_BUZZER_INDEX] & 0x7E)
        | int(enabled)
        | (0 if enabled else 0x80)
    )
    send_and_wait(
        sock,
        pack_command(SET_WORK_MODE, work_mode),
        SET_WORK_MODE_RESPONSE,
        timeout,
    )

    verified_mode = read_work_mode(sock, timeout)
    after = buzzer_state(verified_mode)
    if after != (enabled, enabled, enabled):
        raise RuntimeError("Perubahan ditolak: status buzzer tidak berubah")
    return before, after


def main():
    default_host = os.getenv("RFID_READER_1_HOST") or os.getenv(
        "RFID_READER_HOST", "172.15.5.222"
    )
    parser = argparse.ArgumentParser(
        description="Cek atau ubah suara baca, tulis, dan status receiver RFID SANRAY"
    )
    parser.add_argument("action", choices=["status", "off", "on"])
    parser.add_argument(
        "--mode",
        choices=["client", "server"],
        default=os.getenv("RFID_MODE", "client"),
        help="client: hubungkan ke reader; server: tunggu reader dial ke port ini",
    )
    parser.add_argument("--reader-ip", default=default_host)
    parser.add_argument(
        "--listen-host",
        default=os.getenv("RFID_LISTEN_HOST", "0.0.0.0"),
        help="alamat bind saat --mode server",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("RFID_PORT", "5000")),
        help="port reader (client) atau port listen (server)",
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    try:
        sock = connect_reader(
            args.mode,
            args.reader_ip,
            args.port,
            args.listen_host,
            args.timeout,
        )
        with sock:
            if args.action == "status":
                mode = read_work_mode(sock, args.timeout)
                print(
                    "Pengaturan suara:",
                    format_buzzer_state(buzzer_state(mode)),
                )
                return

            enabled = args.action == "on"
            before, after = set_buzzer(sock, args.timeout, enabled)
            print("Pengaturan sebelum:", format_buzzer_state(before))
            print("Pengaturan sesudah:", format_buzzer_state(after))
            print("Konfigurasi terverifikasi:", "ON" if enabled else "OFF")
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f"Gagal: {exc}\n")


if __name__ == "__main__":
    main()
