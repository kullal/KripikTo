# Konteks kerja agent KripikTo

Saat mulai chat baru atau melanjutkan setelah konteks diringkas, baca
[ringkasan sesi](.agents/MDFile/SESSION_CONTEXT.md), lalu periksa `git status`
dan kode terkait sebelum bekerja. Ringkasan mencatat kondisi saat ditulis;
kode dan data terbaru perlu diverifikasi kembali.

Referensi arsitektur awal:
[KRIPIKTO_V2_6_PHASES.md](.agents/MDFile/KRIPIKTO_V2_6_PHASES.md).
Dokumen tersebut adalah spesifikasi awal v2, bukan bukti bahwa semua fase
sudah selesai. Implementasi scoring/replay saat ini menggunakan versi v3.

## Preferensi dan batas pekerjaan

- Berkomunikasi dalam bahasa Indonesia dengan penjelasan alur yang sederhana.
- Pengguna ingin scan teknikal dapat berjalan tanpa news. Gunakan `--no-news`
  untuk alur tersebut; default news pada `main.py` masih aktif.
- Ukur outcome dan biaya sebelum menyatakan strategi lebih baik atau mengubah
  parameter berdasarkan hasil historis.
- Jangan menganggap skor tinggi, `BREAKOUT`, atau trading plan sebagai izin
  untuk entry. Periksa status entry dan kualitas data.
- Perubahan sebelumnya adalah scanner dan simulasi; tidak ada eksekusi order
  sungguhan yang diotorisasi dalam sesi tersebut.
- Jaga data SQLite, snapshot historis, kredensial, dan konfigurasi yang ada.
  Database dan JSON runtime di `backend/data` diabaikan Git.
- Perbarui ringkasan sesi setelah perubahan penting agar chat berikutnya
  mengetahui hasil, batas validasi, dan pekerjaan yang belum selesai.

## Perintah lokal

Jalankan dari root repo dengan PowerShell dan environment `venv`:

```powershell
.\venv\Scripts\python.exe main.py --no-news
.\venv\Scripts\python.exe -m backend.outcome_tracker
.\venv\Scripts\python.exe -m backend.strategy_comparison
.\venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tiga perintah pertama masih terpisah. Penggabungan atau penjadwalan otomatis
belum diminta maupun diimplementasikan pada akhir sesi yang dirangkum.
