# RFID Backend (Simple) - API Documentation

## Overview

Backend sederhana untuk sistem RFID reader. **Tanpa database, tanpa frontend.** Output tag ditampilkan langsung di **terminal**.

- **Base URL:** `http://<host>:8000`
- **CORS:** Semua origin diizinkan (`*`)

## Cara Menjalankan

> **PENTING:** Pastikan IP address PC Anda berada dalam **segmen yang sama** dengan IP address alat (RFID reader) agar backend dapat terkoneksi ke alat.
> *(Catatan: Default IP alat biasanya **`192.168.1.168`**, sehingga Anda bisa mengatur IP PC ke **`192.168.1.100`** atau segmen `192.168.1.x` yang sejenis)*

```bash
cd be
pip install fastapi uvicorn
python3 main.py
```

Backend akan:
- Jalan di `http://0.0.0.0:8000`
- **Auto-connect** ke RFID reader via TCP port `6000` saat startup
- **Auto-reconnect** jika alat mati/disconnect (retry setiap 3 detik)
- **Print data tag** langsung ke terminal setiap ada tag terdeteksi

## Dependencies

- Python 3.8+
- `fastapi`
- `uvicorn`

---

## Endpoints

### 1. Start Scan

Mulai scan RFID reader.

```
POST /start
```

**Request:** Tidak ada body

**Response:**
```json
{
  "status": "start"
}
```

---

### 2. Stop Scan

Stop scan RFID reader.

```
POST /stop
```

**Request:** Tidak ada body

**Response:**
```json
{
  "status": "stop"
}
```

---

### 3. View Tags

Ambil snapshot data tag yang sedang terdeteksi. Hanya tag aktif yang ditampilkan (yang sudah hilang >5 detik tidak muncul).

```
GET /view
```

**Response:**
```json
{
  "total": 3,
  "tags": [
    {
      "num": 1,
      "id": "0524070019",
      "device_id": "2c070102fffa0d",
      "remove": 0,
      "power": 128,
      "battery": "battereNormal",
      "count": 95,
      "last_time": "2026-04-13 09:45:10.637"
    }
  ]
}
```

**Catatan field:**

| Field     | Type   | Keterangan                                             |
|-----------|--------|--------------------------------------------------------|
| num       | int    | Nomor urut                                             |
| id        | string | RFID tag ID (hex, 10 karakter jika lengkap)            |
| device_id | string | Device ID reader (hex)                                 |
| remove    | int    | 0 = aktif, 1 = sudah hilang                            |
| power     | int    | Power level                                            |
| battery   | string | Status baterai                                         |
| count     | int    | Jumlah kali tag terdeteksi                             |
| last_time | string | Waktu terakhir terdeteksi (`YYYY-MM-DD HH:MM:SS.mmm`) |

---

## Output Terminal

Saat RFID reader aktif scan, data tag akan di-print langsung ke terminal dengan format:

```
num   id           devid            remove power battery        count  lasttime
00001 0524070019   2c070102fffa0d   0        128 battereNormal  000095 2026-04-13 09:45:10.637
================================================================================
```

---

## Alur Penggunaan

```
1. Jalankan backend (python3 main.py)
   → Auto-connect ke RFID reader via TCP port 6000
   → Auto-reconnect jika putus

2. Start scan → POST /start
   → RFID reader mulai scan
   → Data tag muncul di terminal

3. Lihat tag via API → GET /view
   → Tampilkan snapshot tag aktif dalam JSON

4. Stop scan → POST /stop
   → RFID reader berhenti scan
```

---

## Struktur Project

```
rfid/
└── be/
    ├── main.py          # Entry point, FastAPI app, auto-connect
    ├── rfid.py          # Frame parsing, tag management, TCP server
    ├── routes.py        # API endpoint (start, stop, view)
    └── API_DOCS.md      # Dokumentasi ini
```
