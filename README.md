# Ruang Hadir — PWA Absensi Pegawai

Aplikasi absensi pegawai berbasis lokasi, waktu server WITA (GMT+8), dan foto. Backend memakai Python standard library dan SQLite; aplikasi web dapat dipasang sebagai PWA. Catatan absensi hanya dikirim saat online.

## Menjalankan

Memerlukan Python 3.10 atau lebih baru.

```powershell
$env:ADMIN_PASSWORD = "ganti-dengan-kata-sandi-kuat-minimal-12-karakter"
$env:SECRET_KEY = "ganti-dengan-rahasia-acak-yang-panjang"
python server.py
```

Buka `http://localhost:8000`. Nama login administrator adalah `admin` secara default; dapat diubah dengan `ADMIN_USERNAME`. Basis data dibuat di `data/attendance.sqlite3`; ubah lokasinya dengan `ABSEN_DB_PATH`.

Untuk penggunaan di perangkat selain komputer server, pasang aplikasi di balik HTTPS. Kamera dan lokasi browser membutuhkan HTTPS (pengecualian: `localhost`). Atur `COOKIE_SECURE=1` saat koneksi ke server menggunakan HTTPS. Jika server harus menerima koneksi dari jaringan lain, atur `HOST=0.0.0.0` dan lindungi dengan reverse proxy HTTPS; jangan membuka port HTTP langsung ke internet.

Jalankan server dan buka alamat `http://localhost:8000` untuk menggunakan aplikasi secara penuh. Jika `public/index.html` dibuka langsung sebagai file, halaman hanya menampilkan preview dinamis; masuk, kamera, lokasi, dan absensi tetap memerlukan server.

### Akses kamera ponsel melalui Wi-Fi (HTTPS langsung dari Python)

Server dapat melayani HTTPS secara langsung tanpa Laragon atau reverse proxy. Komputer server dan ponsel harus berada di Wi-Fi yang sama. Untuk alamat LAN, browser ponsel harus mempercayai sertifikat lokalnya:

1. Pasang [mkcert](https://github.com/FiloSottile/mkcert) di Windows, misalnya dengan `winget install --id FiloSottile.mkcert -e`, lalu tutup dan buka kembali PowerShell.
2. Dari PowerShell, masuk ke folder proyek dan buat sertifikat untuk IP Wi-Fi server:

   ```powershell
   cd D:\Project\absenpw
   mkcert -install
   New-Item -ItemType Directory -Force .\certs | Out-Null
   $ip = Read-Host "Masukkan IPv4 Wi-Fi komputer server (lihat ipconfig)"
   mkcert -cert-file .\certs\server.pem -key-file .\certs\server-key.pem $ip localhost 127.0.0.1
   ```

3. Tampilkan lokasi root CA mkcert dengan `mkcert -CAROOT`. Salin **hanya** `rootCA.pem` dari folder tersebut ke ponsel dan pasang sebagai sertifikat CA tepercaya melalui pengaturan keamanan ponsel. Pada iPhone/iPad, aktifkan kepercayaan penuh untuk sertifikat tersebut di **Pengaturan → Umum → Mengenai → Pengaturan Kepercayaan Sertifikat**. Jangan salin atau bagikan `rootCA-key.pem` maupun `server-key.pem`.
4. Jalankan aplikasi HTTPS dari PowerShell:

   ```powershell
   $env:HOST = "0.0.0.0"
   $env:PORT = "8000"
   $env:TLS_CERT_FILE = ".\certs\server.pem"
   $env:TLS_KEY_FILE = ".\certs\server-key.pem"
   $env:ADMIN_PASSWORD = "ganti-dengan-kata-sandi-kuat-minimal-12-karakter"
   $env:SECRET_KEY = "ganti-dengan-rahasia-acak-yang-panjang"
   py -3 server.py
   ```

5. Buka `https://<IP-Wi-Fi-server>:8000` di ponsel, menggunakan IP yang sama saat membuat sertifikat. Izinkan Camera dan Location pada prompt browser. Jika koneksi tidak terbuka, izinkan Python pada Windows Firewall untuk jaringan **Private**.

`COOKIE_SECURE` otomatis diaktifkan saat TLS dinyalakan. Jangan meneruskan port server ke internet. Jika IP Wi-Fi server berubah, buat ulang sertifikat untuk IP baru dan perbarui alamat yang dibuka ponsel. Sertifikat root CA bersifat sensitif: lindungi kunci privat CA yang hanya tersimpan pada komputer server.

## Pengaturan admin

Masuk dengan nama admin dan `ADMIN_PASSWORD`, lalu masukkan latitude, longitude, dan radius kantor (10–5.000 meter). Koordinat kantor tidak perlu disimpan di browser pegawai. Setelan tersimpan di database server.

Admin dapat memilih rentang tanggal untuk melihat rekap dan foto yang dikirim, lalu mengunduh ZIP berisi CSV dan foto JPEG.

Di ponsel, buka alamat HTTPS aplikasi lalu gunakan menu browser **Tambahkan ke layar utama** untuk memasangnya sebagai PWA.

## Alur pegawai

Pegawai membuat akun dengan nama, NIP, jabatan, dan kata sandi (minimal 8 karakter), kemudian masuk untuk mengambil foto dan mengizinkan akses lokasi. Foto diubah di browser menjadi JPEG maksimal 200 KiB; server tetap memeriksa ukuran dan format sebelum menyimpan.

Jadwal berdasarkan jam server **WITA (GMT+8, zona IANA `Asia/Makassar`)**:

| Absensi | Waktu |
| --- | --- |
| Datang | 07.30–08.00 |
| Siang | 12.00–13.00 |
| Pulang | 16.00–18.00 |

Server menolak pengiriman di luar jadwal, di luar radius kantor, atau pengiriman kedua untuk pegawai, tanggal, dan periode yang sama.

## Catatan lokasi

Server menghitung jarak dari koordinat GPS yang dikirim browser terhadap titik dan radius yang dikonfigurasi admin. Browser web biasa tidak dapat memberikan bukti kriptografis bahwa koordinat GPS tidak dipalsukan; untuk kebutuhan keamanan tinggi, pertimbangkan kontrol tambahan seperti perangkat terkelola atau aplikasi native.
