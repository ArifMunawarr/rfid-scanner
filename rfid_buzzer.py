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


def pack_command(command, payload=b""):
    body = bytes([command, len(payload)]) + payload
    checksum = sum(body) & 0xFF
    return b"\xBB" + body + bytes([checksum]) + b"\x0D\x0A"


def send_and_wait(host, port, command, expected_response, timeout):
    deadline = time.monotonic() + timeout
    buffer = bytearray()

    with socket.create_connection((host, port), timeout=timeout) as sock:
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


def read_work_mode(host, port, timeout):
    response = send_and_wait(
        host,
        port,
        pack_command(GET_WORK_MODE),
        GET_WORK_MODE_RESPONSE,
        timeout,
    )
    if len(response) < 3 + WORK_MODE_SIZE + 3:
        raise RuntimeError("Respons work mode reader terlalu pendek")
    return bytearray(response[3 : 3 + WORK_MODE_SIZE])


def buzzer_is_on(work_mode):
    return bool(work_mode[READ_BUZZER_INDEX] & 0x01)


def set_buzzer(host, port, timeout, enabled):
    work_mode = read_work_mode(host, port, timeout)
    before = buzzer_is_on(work_mode)

    # Bit 0 mengatur buzzer. Bit 7 (sinkronisasi GPIO0) dipertahankan.
    work_mode[READ_BUZZER_INDEX] = (
        (work_mode[READ_BUZZER_INDEX] & 0x80) | int(enabled)
    )
    send_and_wait(
        host,
        port,
        pack_command(SET_WORK_MODE, work_mode),
        SET_WORK_MODE_RESPONSE,
        timeout,
    )

    verified_mode = read_work_mode(host, port, timeout)
    after = buzzer_is_on(verified_mode)
    if after != enabled:
        raise RuntimeError("Perubahan ditolak: status buzzer tidak berubah")
    return before, after


def main():
    default_host = os.getenv("RFID_READER_1_HOST") or os.getenv(
        "RFID_READER_HOST", "172.15.5.222"
    )
    parser = argparse.ArgumentParser(
        description="Cek atau ubah buzzer baca pada receiver RFID SANRAY"
    )
    parser.add_argument("action", choices=["status", "off", "on"])
    parser.add_argument("--reader-ip", default=default_host)
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("RFID_PORT", "5000"))
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    try:
        if args.action == "status":
            mode = read_work_mode(args.reader_ip, args.port, args.timeout)
            print("Buzzer:", "ON" if buzzer_is_on(mode) else "OFF")
            return

        enabled = args.action == "on"
        before, after = set_buzzer(
            args.reader_ip, args.port, args.timeout, enabled
        )
        print("Buzzer sebelum:", "ON" if before else "OFF")
        print("Buzzer sesudah:", "ON" if after else "OFF")
        print("VERIFIED:", "ON" if after else "OFF")
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f"Gagal: {exc}\n")


if __name__ == "__main__":
    main()
