# KripikTo: Sistem Pemindai Kripto Kuantitatif & Analisis Sentimen AI

RSI scanner dan trigger 15M menggunakan satu implementasi di `backend/feature_engine.py`.
Perhitungan mempertahankan rata-rata rolling gain/loss: warm-up dan harga datar bernilai 50,
kenaikan tanpa penurunan bernilai 100, dan penurunan tanpa kenaikan bernilai 0.
Jalankan tes regresi indikator lokal dengan `python -m unittest discover -s tests -v`.
Hasil scan historis tetap menyimpan nilai indikator saat scan tersebut dibuat; perbaikan
berlaku pada scan berikutnya.

Gerbang entry memprioritaskan `EXTENDED`/`FAILED` sebelum trigger 15M. Lonjakan
candle 15M >=4% tetap `EXTENDED`, dan trigger mikro tidak membatalkan status
`EXTENDED` dari struktur timeframe besar dalam scan yang sama.
`stop_loss_pct` adalah batas kerugian maksimum: SL support/ATR dibatasi nilai
konfigurasi tersebut, dengan pembulatan harga yang menjaga batas risiko.
Outcome tracker mengevaluasi limit pada `entry_price` eksplisit (atau titik tengah
buy area untuk data lama), hanya mencatat fill ketika low/high candle mencakup
harga itu. Fill yang berbenturan dengan TP/SL dalam satu candle menjadi `AMBIGUOUS`.
Evaluasi direkonstruksi dari waktu scan dan memakai time-stop default 6 jam.
Timestamp fill/exit dari OHLC memiliki resolusi candle, bukan waktu transaksi aktual.

Scanner memvalidasi 51 candle struktur, 35 candle momentum 1H, dan 29 candle
trigger 15M terbaru. Candle harus memiliki `is_closed=1`, metadata waktu lengkap,
OHLC/volume valid, dan urutan tanpa duplikasi atau jeda. Umur candle terakhir dan
pengambilannya dibatasi satu interval + toleransi 60 detik; snapshot ticker 24 jam
harus diambil dalam 1 jam + 60 detik. `--skip-download` tetap melewati pemeriksaan ini.
Data historis tetap tersimpan; metadata lama yang tidak diketahui tidak dianggap
valid secara otomatis dan perlu diperbarui lewat unduhan berikutnya.

Relative strength membandingkan return koin dan BTC pada interval yang diminta,
dengan batas waktu candle sekarang dan sebelumnya yang sama persis. Benchmark
yang hilang ditampilkan `N/A`, disimpan sebagai SQL `NULL` / JSON `null`, dan
tidak diganti return BTC nol. Kolom `rs_structure` berlaku pada semua interval;
kolom kompatibilitas `rs_4h` hanya terisi untuk scan 4H. Kekurangan data momentum,
benchmark, atau trigger membuat kandidat `PARTIAL` dan memblokir entry baru.
Alasan penolakan tersimpan di `backend/data/data_quality_latest.json`.

## Replay dan eksperimen strategi (v3)

Skor mentah pilar tetap berskala 25/30/20/15. Kontribusi dihitung dengan
`raw / maksimum_raw * bobot`, memakai helper yang sama di scanner dan kalibrasi.
Klasifikasi setup memakai skor mentah sehingga perubahan bobot tidak mengubah
definisi setup. Skor teknikal dibatasi 90; 10 poin tersedia untuk berita opsional.
Makro menjadi modifier eksplisit (`macro_score - 5`) dan penalti disimpan terpisah.
Snapshot menyimpan `scoring_version`, bobot, kontribusi, support, jenis trigger,
dan level breakout. Konfigurasi kalibrasi lama tanpa validasi versi baru diabaikan;
baseline digunakan sampai konfigurasi baru lolos pemeriksaan.

Jalankan dari direktori proyek dengan environment Python yang dependensinya lengkap:

```powershell
# Scan baru tanpa RSS/Gemini
python main.py --no-news

# Setelah harga bergerak, kumpulkan candle dan evaluasi sinyal yang layak entry
python -m backend.outcome_tracker --fee-bps 10 --slippage-bps 5

# Bandingkan dua entry pada snapshot breakout dan forward window yang sama
python -m backend.strategy_comparison --fee-bps 10 --slippage-bps 5

# Kalibrasi TP/SL memakai replay; --apply hanya menyimpan jika pemeriksaan lolos
python -m backend.calibration_lab
python -m unittest discover -s tests -v
```

Environment lokal yang dibuat untuk pengujian tersedia di `venv/`; pada Windows
gunakan `.\venv\Scripts\python.exe` sebagai pengganti `python` bila `.venv/` lama rusak.

Replay memakai candle 15M tertutup, entry limit maksimum 2 jam, dan time-stop
6 jam sejak candle fill. Candle yang hilang/duplikat ditolak. Waktu fill/exit
tetap perkiraan pada resolusi candle. Fill dan exit dalam candle limit yang sama,
atau TP/SL yang urutannya tidak diketahui, menjadi `AMBIGUOUS` tanpa P&L buatan.
Strategi exit utama menutup seluruh posisi di TP1; TP2 bukan partial exit tersimulasi.
Timeout memakai harga open pada batas waktu, stop yang gap memakai harga open,
dan return observasi 12/24/48H dihitung terpisah jika datanya tersedia.

Fee default **10 bps (0,10%) per sisi** dan slippage **5 bps (0,05%) pada eksekusi
market** adalah asumsi eksperimen yang dapat diubah, bukan tarif resmi bursa.
Limit entry dan TP limit tidak diberi slippage yang melanggar harga limit. Simulasi
touch belum memodelkan antrean order, partial fill, ukuran posisi atau kedalaman book.
Hasil `WAIT`, `EXTENDED`, kualitas data tidak lengkap, dan blokir eksekusi eksplisit
tidak masuk statistik transaksi. Hasil legacy dipisahkan; gunakan `--recheck` bila
ingin memperbarui replay lama yang layak entry. Candle utuh disimpan di tabel
`replay_candles` agar eksperimen exit tidak bergantung pada MFE/MAE yang terpotong.

Perbandingan A/B khusus snapshot `TRIGGERED` + `BREAKOUT` versi baru:
breakout masuk market pada open candle sesudah scan; retest menunggu candle
berikutnya menyentuh resistance yang disimpan dan close bullish di atasnya,
lalu masuk pada open berikutnya. Close di bawah level membatalkan retest.
Keduanya menggunakan stop/target absolut yang sama dan jendela data lengkap
8 jam (tunggu maksimum 2 jam + hold 6 jam). Hasil tersimpan di
`backend/data/strategy_comparison.json`; data tidak cukup dilaporkan secara eksplisit.
Return laporan adalah hasil per trade, bukan return portofolio atau drawdown modal:
sinyal dapat tumpang tindih. Laporan tidak otomatis memilih strategi pemenang.

Kalibrasi memisahkan seluruh timestamp scan dan membuang label train yang
bertumpang tindih dengan awal test. Eksperimen saat ini menguji TP/SL dengan
bobot baseline tetap. Penyimpanan memerlukan minimal 10 trade OOS selesai,
profit factor finite >1 dan expectancy bersih positif. Ini pemeriksaan minimum,
bukan bukti bahwa strategi sudah optimal atau bebas overfitting.

Sistem pemindai pasar aset kripto otomatis berbasis kombinasi data **Kuantitatif (Candlestick Klines + Whale Flow / Bandarmologi Paus)** dan **Kualitatif (Analisis Sentimen Berita Global dengan Google Gemini Flash LLM + Crypto Risk Guard)**.

---

## 🎯 Tujuan Proyek

1. **Memantau Top 500 Koin Likuid (Pasangan USDT)**: Menyaring koin-koin aktif di bursa global (berdasarkan nilai transaksi/turnover USDT 24 jam) agar terbebas dari sinyal palsu pada koin mati (*zombie/illiquid coins*).
2. **Penyaringan Otomatis Aset Non-Kripto**: Otomatis menyingkirkan pasangan *stablecoin-to-stablecoin* (seperti USDC, FDUSD, USDS), token saham AS ter-tokenisasi (*Apple, Tesla, Nvidia tokens*), dan emas (*XAUT*).
3. **Deteksi Berbasis Kuantitatif, Whale Flow (*Smart Money Inflow*) & Short Squeeze**:
   - Menggunakan metrik **Taker Buy Ratio** (persentase pembelian pasar agresif "hajar kanan").
   - Deteksi *Whale Divergence*: Mendeteksi saat harga koin masih datar / koreksi tipis, tetapi paus memborong agresif di balik layar.
   - **Funding Rate & Short Squeeze Detector**: Memantau suku bunga pendanaan Binance Futures. Funding rate negatif ekstrem (misal `-0.10%` s.d `-0.90%`) mendeteksi posisi Short yang terjebak, menjadi bahan bakar lonjakan harga instan untuk spot scalping.
   - Indikator teknikal: **MA20, MA50, Breakout 20-bar High, Volume Spike (>1.5x - 2x)**, dan **RSI 14** (Momentum sehat vs risiko pucuk overbought).
4. **Crypto Risk Guard & Konfirmasi Berita AI (Google Gemini Flash)**:
   - Mengambil headline berita global terkini via Google News RSS (*CoinDesk, Cointelegraph, Decrypt*, dll.).
   - Mendeteksi risiko fatal: **Hack / Smart Contract Exploit, Gugatan Regulasi (SEC/CFTC), Pengumuman Delisting, atau Token Dumping**.
   - Memberikan skor sentimen (-1.0 s.d +1.0) dan ringkasan katalis berita dalam **Bahasa Indonesia**.
5. **Trading Plan Khusus Spot Scalping (Target Profit +6.0%)**:
   - Menghitung **Buy Area**, **Stop Loss Ketat (~3.8%)**, **TP 1 Utama (+6.0% langsung kunci keuntungan spot)**, dan **TP 2 (+12.0%)**. Tidak menggunakan leverage futures sehingga aman dari likuidasi.

---

## 🏗️ Arsitektur Data

| Komponen | Sumber Data | Keterangan |
|---|---|---|
| **Sentimen Makro Global** | Alternative.me API | **Crypto Fear & Greed Index** (Rezim pasar: Extreme Fear, Fear, Neutral, Greed, Extreme Greed). |
| **Derivatif & Short Squeeze** | CoinGecko Public Derivatives API | Funding Rate & Open Interest Binance Futures (Akses lancar di Indonesia tanpa VPN/Proxy). |
| **Data Pasar 24 Jam** | Binance Public Vision API | Endpoint resmi `https://data-api.binance.vision` (Bebas akses di Indonesia tanpa VPN & tanpa API key). |
| **Candlestick (Klines)** | Binance Public Vision API | Mendukung interval fleksibel: `2h` (default), `4h`, atau `1d` (limit 100 candle bergulir). |
| **Whale Flow (Aliran Paus)** | Taker Buy Quote Volume | Menghitung rasio transaksi beli agresif (*market order*) terhadap total volume transaksi. |
| **Universe Selection** | Filter Likuiditas USDT | Top 500 koin USDT dengan volume terbesar, bebas stablecoin & token leverage. |
| **Berita Global** | Google News RSS Kripto | Mengambil berita terkini dari portal media kripto terkemuka di dunia. |
| **Sentimen AI & Risk Guard** | Google Gemini Flash LLM | Model `gemini-flash-latest` / `gemini-3.8-flash` dengan respon JSON terstruktur. |
| **Penyimpanan Lokal** | SQLite (`kripto.db`) & JSON | Penyimpanan lokal yang cepat, ringan, dan efisien di `backend/data/`. |

---

## 📁 Struktur Folder Proyek

```text
KripikTo/
├── backend/
│   ├── data/
│   │   ├── kripto.db                  # Database SQLite lokal (Summary 24h, Klines, Scan Results, Sentiment, Derivatives)
│   │   ├── scan_latest.json          # Hasil scan teknikal & whale flow & funding rate
│   │   └── final_recommendations.json # Hasil rekomendasi final + Trading Plan (+6% TP) + Katalis AI
│   ├── macro_sentiment.py            # Tahap 0: Barometer Sentimen Makro (Fear & Greed Index & Rezim Pasar)
│   ├── derivatives_flow.py           # Tahap 0.5: Pelacak Derivatif & Funding Rate Binance Futures (Short Squeeze)
│   ├── data_pipeline.py              # Tahap 1: Pipa unduh data Binance Vision (Multithreading cepat)
│   ├── scanner.py                    # Tahap 2: Logika deteksi teknikal, RSI 14, Whale Inflow, & Short Squeeze
│   ├── news_sentiment.py             # Tahap 3: Scraper berita kripto global & Analisis Sentimen Gemini LLM
│   └── __init__.py
├── main.py                           # Orkestrator utama: CLI terpadu
├── .env                              # Kunci API Gemini (Terproteksi .gitignore)
├── .env.example                      # Template konfigurasi environment
├── .gitignore                        # Proteksi venv, db, dan kredensial
├── .venv/                            # Python Virtual Environment
├── requirements.txt                  # Daftar dependensi paket Python minimal
└── README.md                         # Dokumentasi & panduan penggunaan sistem
```


---

## 🚀 Cara Menjalankan Sistem (`main.py`)

Pastikan virtual environment telah aktif:
```bash
# Di Windows PowerShell:
.\.venv\Scripts\Activate.ps1
```

Cukup jalankan satu perintah berikut di terminal:

```bash
# 1. Jalankan alur penuh (Unduh 500 koin -> Scan -> Analisis Berita Gemini AI):
python main.py

# 2. Mengubah Timeframe Candlestick (misal grafik 4 jam atau harian):
python main.py --interval 4h
python main.py --interval 1d

# 3. Mengatur jumlah koin teratas yang dianalisis beritanya oleh AI (misal Top 10 atau Top 25):
python main.py --top 10

# 4. Mode Cepat / Hemat Kuota (Melewati unduhan jika data baru saja diunduh hari ini):
python main.py --skip-download --top 10

# 5. Scan teknikal tanpa berita RSS / Gemini (tidak memerlukan GEMINI_API_KEY):
python main.py --no-news
```

Saat memakai `--no-news`, hasil teknikal dan Trading Plan tersedia di
`backend/data/scan_latest.json` serta SQLite. `final_recommendations.json`
tidak diperbarui karena tahap berita dilewati. Dari Python, gunakan
`run_all(news_enabled=False)` untuk melewati berita atau
`run_all(news_enabled=True)` untuk mengaktifkannya (default).

---

## 📊 Bobot Penilaian Skor Akhir (Composite Scoring)

- **Bobot Teknikal & Whale Flow**: `70%`
- **Bobot Sentimen Berita AI**: `30%`
- **Crypto Risk Guard Shield**: Jika terdeteksi berita peretasan (*hack*), eksploitasi, tuntutan keras SEC, atau delisting bursa, skor akhir **otomatis dipangkas maksimal ke angka 35** dan status rekomendasi diubah menjadi **`⚠️ AVOID (Hack / Exploit / Delisting Risk)`** untuk melindungi modal trader dari jebakan harga semu (*bull trap*).
