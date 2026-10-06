from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import math
import os
import secrets
import sqlite3
import ssl
import sys
import time
import traceback
import zipfile
from contextlib import contextmanager
from datetime import date, datetime, time as clock_time, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
DB_PATH = Path(os.environ.get("ABSEN_DB_PATH", str(ROOT / "data" / "attendance.sqlite3")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32)).encode("utf-8")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
TLS_CERT_FILE = os.environ.get("TLS_CERT_FILE", "").strip()
TLS_KEY_FILE = os.environ.get("TLS_KEY_FILE", "").strip()
TIMEZONE = timezone(timedelta(hours=8), "WITA")
MAX_PHOTO_BYTES = 200 * 1024
MAX_REQUEST_BYTES = 3 * 1024 * 1024
PERIODS = (
    ("datang", "Absen datang", clock_time(7, 30), clock_time(8, 1)),
    ("siang", "Absen siang", clock_time(12, 0), clock_time(13, 1)),
    ("pulang", "Absen pulang", clock_time(16, 0), clock_time(18, 1)),
)


class AppError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def connect_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


@contextmanager
def database():
    db = connect_db()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def initialize_db() -> None:
    with database() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                nip TEXT NOT NULL UNIQUE,
                position TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                attendance_date TEXT NOT NULL,
                period TEXT NOT NULL CHECK (period IN ('datang', 'siang', 'pulang')),
                taken_at TEXT NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                distance_m REAL NOT NULL,
                photo BLOB NOT NULL,
                photo_type TEXT NOT NULL,
                UNIQUE (employee_id, attendance_date, period)
            );
            """
        )
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS employees_nip_ci ON employees(nip COLLATE NOCASE)")


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        salt_hex, expected = encoded.split(":", 1)
        actual = hash_password(password, bytes.fromhex(salt_hex)).split(":", 1)[1]
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def session_token(subject: str, role: str) -> str:
    payload = json.dumps(
        {"sub": subject, "role": role, "exp": int(time.time()) + 12 * 60 * 60},
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = hmac.new(SECRET_KEY, encoded, hashlib.sha256).digest()
    return (encoded + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode("ascii")


def read_session(token: str) -> dict | None:
    try:
        encoded, signature = token.encode("ascii").split(b".", 1)
        expected = hmac.new(SECRET_KEY, encoded, hashlib.sha256).digest()
        supplied = base64.urlsafe_b64decode(signature + b"=" * (-len(signature) % 4))
        if not hmac.compare_digest(expected, supplied):
            return None
        payload = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
        session = json.loads(payload)
        if int(session["exp"]) <= int(time.time()):
            return None
        return session
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def office_settings(db: sqlite3.Connection) -> dict:
    rows = db.execute("SELECT key, value FROM settings").fetchall()
    values = {row["key"]: row["value"] for row in rows}
    try:
        latitude = float(values["latitude"])
        longitude = float(values["longitude"])
        radius = int(values.get("radius_m", "100"))
        configured = -90 <= latitude <= 90 and -180 <= longitude <= 180 and 10 <= radius <= 5000
    except (KeyError, ValueError):
        latitude, longitude, radius, configured = None, None, 100, False
    return {"configured": configured, "radius_m": radius, "latitude": latitude, "longitude": longitude}


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    value = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    value = min(1, max(0, value))
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def active_period(now: datetime) -> tuple[str, str] | None:
    current_time = now.timetz().replace(tzinfo=None)
    for key, label, start, end_exclusive in PERIODS:
        if start <= current_time < end_exclusive:
            return key, label
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "AbsensiPWA/1.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_POST(self) -> None:
        try:
            self.check_origin()
            body = self.read_json()
            path = urlsplit(self.path).path
            if path == "/api/register":
                self.register(body)
            elif path == "/api/login":
                self.login(body)
            elif path == "/api/logout":
                self.send_response(HTTPStatus.OK)
                self.set_cookie("", clear=True)
                self.send_json_body({"ok": True})
            elif path == "/api/attendance":
                self.submit_attendance(body)
            elif path == "/api/admin/settings":
                self.save_settings(body)
            else:
                raise AppError(404, "Halaman tidak ditemukan.")
        except AppError as error:
            self.send_json(error.status, {"error": error.message})
        except sqlite3.IntegrityError:
            self.send_json(409, {"error": "NIP atau absen pada periode ini sudah terdaftar."})
        except Exception:
            self.log_error("POST request failed:\n%s", traceback.format_exc())
            self.send_json(500, {"error": "Terjadi kesalahan server. Silakan coba lagi."})

    def do_HEAD(self) -> None:
        self.do_GET()

    def send_json(self, status: int, payload: dict) -> None:
        self.send_response(status)
        self.send_json_body(payload)

    def send_json_body(self, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def check_origin(self) -> None:
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            expected_scheme = "https" if COOKIE_SECURE else "http"
            if parsed.netloc != self.headers.get("Host") or parsed.scheme != expected_scheme:
                raise AppError(403, "Permintaan lintas situs ditolak.")

    def read_json(self) -> dict:
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise AppError(415, "Permintaan harus menggunakan JSON.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise AppError(400, "Ukuran permintaan tidak valid.")
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise AppError(413, "Ukuran permintaan tidak valid atau terlalu besar.")
        try:
            data = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise AppError(400, "Format data tidak valid.")
        if not isinstance(data, dict):
            raise AppError(400, "Format data tidak valid.")
        return data

    def session(self) -> dict | None:
        cookie = self.headers.get("Cookie", "")
        token = next((part.strip()[6:] for part in cookie.split(";") if part.strip().startswith("absen=")), "")
        return read_session(token) if token else None

    def require_role(self, role: str) -> dict:
        session = self.session()
        if not session or session.get("role") != role:
            raise AppError(401, "Silakan masuk kembali.")
        return session

    def set_cookie(self, token: str, clear: bool = False) -> None:
        value = f"absen={token}; Path=/; HttpOnly; SameSite=Strict"
        if COOKIE_SECURE:
            value += "; Secure"
        if clear:
            value += "; Max-Age=0"
        else:
            value += "; Max-Age=43200"
        self.send_header("Set-Cookie", value)

    def handle_api_get(self, path: str) -> None:
        with database() as db:
            if path == "/api/config":
                self.send_json(200, {"office": office_settings(db)})
                return
            if path == "/api/session":
                session = self.session()
                if session and session.get("role") == "employee":
                    employee = db.execute(
                        "SELECT id, name, nip, position FROM employees WHERE id = ?",
                        (int(session["sub"]),),
                    ).fetchone()
                    if employee:
                        self.send_json(200, {"role": "employee", "employee": dict(employee)})
                        return
                elif session and session.get("role") == "admin":
                    self.send_json(200, {"role": "admin"})
                    return
                self.send_json(200, {"role": "guest"})
                return
            if path == "/api/attendance/today":
                session = self.require_role("employee")
                today = datetime.now(TIMEZONE).date().isoformat()
                rows = db.execute(
                    "SELECT period, taken_at FROM attendance WHERE employee_id = ? AND attendance_date = ?",
                    (int(session["sub"]), today),
                ).fetchall()
                self.send_json(200, {"date": today, "records": [dict(row) for row in rows]})
                return
            if path == "/api/admin/report":
                self.require_role("admin")
                query = urlsplit(self.path).query
                from urllib.parse import parse_qs
                params = parse_qs(query)
                start, end = params.get("from", [""])[0], params.get("to", [""])[0]
                start_date, end_date = self.report_dates(start, end)
                rows = db.execute(
                    """SELECT a.id, e.name, e.nip, e.position, a.attendance_date,
                              a.period, a.taken_at, a.latitude, a.longitude, a.distance_m
                       FROM attendance a JOIN employees e ON e.id = a.employee_id
                       WHERE a.attendance_date BETWEEN ? AND ?
                       ORDER BY a.attendance_date DESC, e.name COLLATE NOCASE, a.taken_at""",
                    (start_date, end_date),
                ).fetchall()
                self.send_json(200, {"records": [dict(row) for row in rows]})
                return
            if path.startswith("/api/admin/photo/"):
                self.require_role("admin")
                try:
                    record_id = int(path.rsplit("/", 1)[1])
                except ValueError:
                    raise AppError(404, "Foto tidak ditemukan.")
                row = db.execute(
                    "SELECT photo, photo_type FROM attendance WHERE id = ?", (record_id,)
                ).fetchone()
                if not row:
                    raise AppError(404, "Foto tidak ditemukan.")
                self.send_bytes(200, bytes(row["photo"]), row["photo_type"], "private, no-store")
                return
            if path == "/api/admin/settings":
                self.require_role("admin")
                self.send_json(200, {"office": office_settings(db)})
                return
        raise AppError(404, "Halaman tidak ditemukan.")

    def report_dates(self, start: str, end: str) -> tuple[str, str]:
        try:
            parsed_start, parsed_end = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError:
            raise AppError(400, "Tanggal awal dan akhir wajib diisi dengan format yang benar.")
        if parsed_start.isoformat() != start or parsed_end.isoformat() != end:
            raise AppError(400, "Tanggal harus menggunakan format YYYY-MM-DD.")
        if parsed_end < parsed_start or (parsed_end - parsed_start).days > 366:
            raise AppError(400, "Rentang tanggal tidak valid (maksimal 366 hari).")
        return parsed_start.isoformat(), parsed_end.isoformat()

    def register(self, body: dict) -> None:
        name = self.clean_text(body.get("name"), "Nama", 100)
        nip = self.clean_text(body.get("nip"), "NIP", 30)
        position = self.clean_text(body.get("position"), "Jabatan", 100)
        password = body.get("password", "")
        if not isinstance(password, str) or len(password) < 8 or len(password) > 128:
            raise AppError(400, "Kata sandi harus terdiri dari 8 hingga 128 karakter.")
        if not all(char.isalnum() or char in "-_." for char in nip):
            raise AppError(400, "NIP hanya boleh berisi huruf, angka, titik, tanda hubung, atau garis bawah.")
        with database() as db:
            cursor = db.execute(
                "INSERT INTO employees (name, nip, position, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, nip, position, hash_password(password), datetime.now(TIMEZONE).isoformat()),
            )
            employee_id = cursor.lastrowid
        self.send_response(201)
        self.set_cookie(session_token(str(employee_id), "employee"))
        self.send_json_body({"role": "employee", "employee": {"id": employee_id, "name": name, "nip": nip, "position": position}})

    def login(self, body: dict) -> None:
        username = self.clean_text(body.get("username"), "NIP / admin", 100)
        password = body.get("password", "")
        if not isinstance(password, str) or len(password) > 128:
            raise AppError(400, "NIP atau kata sandi tidak valid.")
        if username.casefold() == ADMIN_USERNAME.casefold() and verify_password(password, hash_password(ADMIN_PASSWORD, b"absen-admin-salt")):
            role, subject = "admin", ADMIN_USERNAME
        else:
            with database() as db:
                employee = db.execute(
                    "SELECT id, name, nip, position, password_hash FROM employees WHERE nip = ? COLLATE NOCASE",
                    (username,),
                ).fetchone()
            if not employee or not verify_password(password, employee["password_hash"]):
                raise AppError(401, "NIP atau kata sandi salah.")
            role, subject = "employee", str(employee["id"])
        self.send_response(200)
        self.set_cookie(session_token(subject, role))
        self.send_json_body({"role": role, "employee": dict(employee) if role == "employee" else None})

    def submit_attendance(self, body: dict) -> None:
        session = self.require_role("employee")
        now = datetime.now(TIMEZONE)
        period = active_period(now)
        if not period:
            raise AppError(403, "Saat ini di luar jadwal absensi (datang 07.30–08.00, siang 12.00–13.00, pulang 16.00–18.00 WITA).")
        period_key, period_label = period
        try:
            latitude, longitude = float(body.get("latitude")), float(body.get("longitude"))
        except (TypeError, ValueError):
            raise AppError(400, "Lokasi perangkat tidak valid.")
        if not math.isfinite(latitude) or not math.isfinite(longitude) or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise AppError(400, "Lokasi perangkat tidak valid.")
        data_url = body.get("photo")
        prefix = "data:image/jpeg;base64,"
        if not isinstance(data_url, str) or not data_url.startswith(prefix):
            raise AppError(400, "Foto wajib berupa JPEG.")
        encoded = data_url[len(prefix):]
        if len(encoded) > ((MAX_PHOTO_BYTES + 2) // 3) * 4 + 8:
            raise AppError(413, "Ukuran foto melebihi 200 KB.")
        try:
            photo = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error):
            raise AppError(400, "Format foto tidak valid.")
        if len(photo) > MAX_PHOTO_BYTES:
            raise AppError(413, "Ukuran foto melebihi 200 KB.")
        if len(photo) < 4 or not photo.startswith(b"\xff\xd8\xff") or not photo.endswith(b"\xff\xd9"):
            raise AppError(400, "Data foto JPEG tidak valid.")
        with database() as db:
            office = office_settings(db)
            if not office["configured"]:
                raise AppError(503, "Lokasi kantor belum diatur admin.")
            distance = distance_m(latitude, longitude, office["latitude"], office["longitude"])
            if distance > office["radius_m"]:
                raise AppError(403, f"Anda berada di luar radius kantor ({round(distance)} m dari kantor).")
            db.execute(
                """INSERT INTO attendance
                   (employee_id, attendance_date, period, taken_at, latitude, longitude, distance_m, photo, photo_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'image/jpeg')""",
                (int(session["sub"]), now.date().isoformat(), period_key, now.isoformat(timespec="seconds"),
                 latitude, longitude, distance, photo),
            )
        self.send_json(201, {"ok": True, "period": period_key, "label": period_label, "taken_at": now.isoformat(timespec="seconds"), "distance_m": round(distance)})

    def save_settings(self, body: dict) -> None:
        self.require_role("admin")
        try:
            latitude, longitude = float(body.get("latitude")), float(body.get("longitude"))
            radius = int(body.get("radius_m"))
        except (TypeError, ValueError):
            raise AppError(400, "Koordinat dan radius harus berupa angka.")
        if not math.isfinite(latitude) or not math.isfinite(longitude) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise AppError(400, "Koordinat tidak valid.")
        if not 10 <= radius <= 5000:
            raise AppError(400, "Radius harus antara 10 hingga 5.000 meter.")
        with database() as db:
            db.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (("latitude", str(latitude)), ("longitude", str(longitude)), ("radius_m", str(radius))),
            )
        self.send_json(200, {"ok": True, "office": {"configured": True, "latitude": latitude, "longitude": longitude, "radius_m": radius}})

    @staticmethod
    def clean_text(value: object, field: str, limit: int) -> str:
        if not isinstance(value, str):
            raise AppError(400, f"{field} wajib diisi.")
        value = value.strip()
        if not value or len(value) > limit or any(ord(char) < 32 for char in value):
            raise AppError(400, f"{field} wajib diisi (maksimal {limit} karakter).")
        return value

    def send_bytes(self, status: int, data: bytes, content_type: str, cache: str = "no-store", filename: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def serve_static(self, path: str) -> None:
        if path == "/":
            path = "/index.html"
        if path == "/admin":
            path = "/index.html"
        target = (PUBLIC / path.lstrip("/")).resolve()
        if PUBLIC.resolve() not in target.parents and target != PUBLIC.resolve():
            raise AppError(404, "Halaman tidak ditemukan.")
        if not target.is_file():
            raise AppError(404, "Halaman tidak ditemukan.")
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".webmanifest": "application/manifest+json",
            ".svg": "image/svg+xml",
        }.get(target.suffix, "application/octet-stream")
        cache = "no-cache" if target.suffix in (".html", ".css", ".js", ".webmanifest") else "public, max-age=3600"
        self.send_bytes(200, target.read_bytes(), content_type, cache)

    def do_GET(self) -> None:
        try:
            path = unquote(urlsplit(self.path).path)
            if path == "/api/admin/export":
                self.export_report()
            elif path.startswith("/api/"):
                self.handle_api_get(path)
            else:
                self.serve_static(path)
        except AppError as error:
            self.send_json(error.status, {"error": error.message})
        except Exception:
            self.log_error("GET request failed:\n%s", traceback.format_exc())
            self.send_json(500, {"error": "Terjadi kesalahan server. Silakan coba lagi."})

    def export_report(self) -> None:
        self.require_role("admin")
        query = urlsplit(self.path).query
        from urllib.parse import parse_qs
        params = parse_qs(query)
        start, end = self.report_dates(params.get("from", [""])[0], params.get("to", [""])[0])
        with database() as db:
            rows = db.execute(
                """SELECT a.id, e.name, e.nip, e.position, a.attendance_date,
                          a.period, a.taken_at, a.latitude, a.longitude, a.distance_m, a.photo
                   FROM attendance a JOIN employees e ON e.id = a.employee_id
                   WHERE a.attendance_date BETWEEN ? AND ?
                   ORDER BY a.attendance_date, e.name COLLATE NOCASE, a.taken_at""",
                (start, end),
            ).fetchall()
        csv_buffer = io.StringIO(newline="")
        writer = csv.writer(csv_buffer)
        writer.writerow(("Nama", "NIP", "Jabatan", "Tanggal", "Periode", "Waktu (WITA)", "Latitude", "Longitude", "Jarak dari kantor (m)", "Foto"))
        period_names = {key: label for key, label, _, _ in PERIODS}
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for row in rows:
                photo_name = f"foto/{row['attendance_date']}_{row['nip']}_{row['period']}.jpg"
                writer.writerow(tuple(self.csv_text(value) for value in (
                    row["name"], row["nip"], row["position"], row["attendance_date"],
                    period_names[row["period"]], row["taken_at"],
                )) + (row["latitude"], row["longitude"], round(row["distance_m"]), photo_name))
                bundle.writestr(photo_name, bytes(row["photo"]))
            bundle.writestr("rekap-absensi.csv", "\ufeff" + csv_buffer.getvalue())
        self.send_bytes(200, archive.getvalue(), "application/zip", "no-store", f"rekap-absensi-{start}-{end}.zip")

    @staticmethod
    def csv_text(value: str) -> str:
        if value.startswith(("=", "+", "-", "@", "\t", "\r")):
            return "'" + value
        return value


def main() -> None:
    global COOKIE_SECURE
    if not ADMIN_USERNAME:
        raise SystemExit("ADMIN_USERNAME wajib diisi.")
    if not ADMIN_PASSWORD or len(ADMIN_PASSWORD) < 12:
        raise SystemExit("Atur ADMIN_PASSWORD dengan minimal 12 karakter sebelum menjalankan server.")
    if len(SECRET_KEY) < 32:
        raise SystemExit("SECRET_KEY harus berisi minimal 32 byte.")
    initialize_db()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    if bool(TLS_CERT_FILE) != bool(TLS_KEY_FILE):
        raise SystemExit("Atur TLS_CERT_FILE dan TLS_KEY_FILE bersama-sama, atau kosongkan keduanya.")
    server = ThreadingHTTPServer((host, port), Handler)
    scheme = "http"
    if TLS_CERT_FILE:
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(certfile=TLS_CERT_FILE, keyfile=TLS_KEY_FILE)
        except (OSError, ssl.SSLError) as error:
            server.server_close()
            raise SystemExit(f"Sertifikat HTTPS tidak dapat dimuat: {error}") from error
        server.socket = context.wrap_socket(server.socket, server_side=True)
        COOKIE_SECURE = True
        scheme = "https"
    print(f"Absensi PWA melayani {scheme.upper()} pada {host}:{port} (zona waktu WITA, GMT+8)")
    if host in ("0.0.0.0", "::"):
        print(f"Di perangkat lain, buka {scheme}://<IP-Wi-Fi-server>:{port}")
    else:
        print(f"Buka aplikasi di {scheme}://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer dihentikan.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
