# KripikTo v2: Scanner Kripto Kuantitatif & Sentimen Berita AI

KripikTo adalah aplikasi CLI Python untuk menyaring pasangan kripto USDT likuid, menganalisis struktur dan momentum pasar, mengonfirmasi timing entry, serta menambahkan analisis headline berita dengan Google Gemini. Hasil berupa laporan terminal, snapshot SQLite, dan JSON lokal. Aplikasi tidak mengirim order ke bursa.

Pembagian timeframe default:

```text
4H: struktur pasar → 1H: momentum → 15M: trigger entry → berita AI & laporan final
```

Skor kondisi pasar, jenis setup, dan status entry merupakan informasi terpisah. Skor tinggi tidak otomatis menghasilkan label beli. Spesifikasi pengembangan tersedia di [.agents/MDFile/KRIPIKTO_V2_6_PHASES.md](.agents/MDFile/KRIPIKTO_V2_6_PHASES.md); README ini menjelaskan implementasi saat ini.

## Instalasi dan konfigurasi

Jalankan dari folder root repo menggunakan Python yang kompatibel dengan dependensi dalam `requirements.txt`. Contoh di Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Jika `.env` sudah ada, gunakan file tersebut. Isi kunci Gemini:

```dotenv
GEMINI_API_KEY=isi_kunci_api_kamu
# Opsional; default gemini-flash-latest
GEMINI_MODEL=gemini-flash-latest
```

`GEMINI_API_KEY` dari environment variable diprioritaskan atas `.env`. Tanpa kunci, scanner kuantitatif tetap berjalan, tetapi tahap berita mengembalikan hasil kosong dan mengosongkan JSON rekomendasi final agar hasil lama tidak terbaca sebagai hasil baru. `.env`, virtual environment, database, dan JSON di `backend/data/` diabaikan oleh Git.

## Menjalankan scanner

```powershell
# Default: hingga 200 koin, struktur 4H, momentum 1H, trigger 15M,
# dan hingga 15 kandidat untuk analisis berita.
python main.py

# Perbesar universe dan jumlah kandidat akhir.
python main.py --liquid 500 --top 25

# Ubah interval struktur; momentum tetap 1H dan trigger tetap 15M.
python main.py --interval 2h --top 10
python main.py --interval 1d

# Gunakan data struktur/momentum lokal yang masih lolos validasi freshness.
python main.py --skip-download --top 10

# Batasi unduhan klines universe pada interval struktur pilihan.
python main.py --no-mtf

python main.py --help
```

| Opsi | Default | Perilaku |
|---|---|---|
| `--interval` | `4h` | Interval struktur: `15m`, `1h`, `2h`, `4h`, atau `1d`. |
| `--top` | `15` | Batas kandidat hasil scanner dan analisis berita. |
| `--liquid` | `200` | Batas pasangan USDT berdasarkan quote volume 24 jam. |
| `--skip-download` | Nonaktif | Melewati `run_pipeline`; validasi data lokal tetap berlaku. |
| `--no-mtf` | Nonaktif | Menonaktifkan unduhan multi-timeframe universe di pipeline. |

**`--skip-download` bukan mode offline.** Makro, derivatif, status simbol spot, trigger 15M kandidat, RSS, dan Gemini masih dapat mengakses jaringan. Data yang diunduh sebelumnya pada hari yang sama belum tentu cukup baru untuk lolos validasi.

**`--no-mtf` hanya mengubah unduhan pipeline.** Scanner tetap membutuhkan momentum 1H dan benchmark yang selaras, serta tetap mengevaluasi trigger 15M. Benchmark BTC juga masih diunduh oleh pipeline. Jika data 1H lokal tidak lengkap atau sudah kedaluwarsa, kandidat menjadi `PARTIAL` dan entry diblokir.

## Alur dan sumber data

| Tahap | Modul | Peran dan sumber |
|---|---|---|
| Makro | `macro_sentiment.py` | Fear & Greed Index dari Alternative.me; menghasilkan rezim pasar dan konteks analisis. |
| Unduhan | `data_pipeline.py` | Ticker 24 jam, status spot, klines, dan benchmark BTC dari `https://data-api.binance.vision`. |
| Integritas | `data_integrity.py` | Validasi candle tertutup, freshness, OHLCV, dan keselarasan return BTC. |
| Derivatif | `derivatives_flow.py` | Funding rate dan open interest Binance Futures dari CoinGecko; menyimpan history untuk delta OI/funding. Dipanggil oleh scanner. |
| Fitur | `feature_engine.py` | RSI, momentum 1H, klasifikasi setup/status, trigger 15M, dan TP/SL dinamis. |
| Scanner | `scanner.py` | Skor v1/v2, trading plan, konfirmasi 15M, pemeringkatan, dan snapshot hasil. |
| Berita | `news_sentiment.py` | Google News RSS per simbol, analisis headline Gemini, Risk Guard, dan skor final. |
| Evaluasi | `outcome_tracker.py` | Rekonstruksi outcome historis dari klines Binance; dijalankan terpisah. |
| Kalibrasi | `calibration_lab.py` | Eksperimen parameter ATR dan bobot menggunakan data outcome; dijalankan terpisah. |

Universe disaring untuk mengecualikan stablecoin, pasangan non-kripto yang terdaftar dalam filter, dan token leverage. Taker buy ratio merupakan persentase taker buy quote volume terhadap total quote volume; metrik ini digunakan sebagai proxy aliran beli agresif, bukan identifikasi transaksi paus individual.

Pipeline meminta 100 candle per interval melalui `main.py`. Mode MTF mengunduh `4h` dan `1h`, ditambah interval struktur pilihan jika berbeda. Scanner mengunduh candle 15M untuk kandidat sebelum memilih hasil akhir.

## Skor v2 dan skor berita

Scanner menyimpan `v1_score` untuk pembandingan historis. Kolom `score` pada hasil scan mengikuti `v2_score`.

| Komponen scanner | Batas poin default | Fitur utama |
|---|---:|---|
| Struktur | 25 | MA20/MA50, breakout, dan relative strength pada interval struktur. |
| Momentum | 30 | ROC dan akselerasi 1H, ekspansi ATR, volume, RSI, dan relative strength 1H. |
| Flow | 20 | Taker buy ratio dan divergence terhadap perubahan harga. |
| Derivatif | 15 | Funding rate, open interest, dan delta OI/funding. |
| Berita | 10 | Headline terbaru yang lolos validasi Gemini; ditambahkan pada tahap berita. |

`v2_score` menjumlahkan empat komponen kuantitatif setelah penalti risiko, maksimum 90 pada bobot default. Makro menjadi konteks analisis, tanpa tambahan poin. `main.py` memakai ambang minimum scanner 40. Setiap komponen dihitung dalam unit baseline, lalu dinormalisasi terhadap bobot konfigurasi; ambang klasifikasi setup tetap memakai unit baseline. Bobot kelima pilar wajib berjumlah 100.

Tahap berita **menambahkan** poin berdasarkan skor sentimen Gemini:

| Skor sentimen | Poin berita |
|---|---:|
| `>= 0.6` | 10 |
| `>= 0.2` dan `< 0.6` | 8 |
| `>= -0.1` dan `< 0.2` | 5 |
| `>= -0.5` dan `< -0.1` | 2 |
| `< -0.5` | 0 |

```text
final_score = clamp(v2_score + weights["news"] × poin_berita / 10, 0, 100)
Jika crypto_risk aktif: final_score dibatasi maksimal 35.
```

Versi scoring saat ini adalah `v2.1-normalized`. Konfigurasi kalibrasi dan skor historis dari versi lama tidak dicampur dengan perhitungan baru. Berita berstatus tidak diketahui memberikan 0 poin tambahan; sentimen netral yang berhasil divalidasi memberikan 5 dari 10 poin berita.

Scanner memprioritaskan status entry (`TRIGGERED`, `READY`, `WAIT`, `EXTENDED`, `FAILED`), kemudian skor v2, taker buy ratio, dan likuiditas. Laporan berita mengurutkan kembali kandidat berdasarkan `final_score`.

## Setup dan gerbang entry

Jenis setup yang tersedia: `ACCUMULATION_COIL`, `MOMENTUM_FORMING`, `MOMENTUM_RUNNER`, `VOLATILITY_SQUEEZE`, `STRUCTURE_BULLISH`, dan `NO_SETUP`.

| Status entry | Arti |
|---|---|
| `WAIT` | Belum ada pemicu entry yang valid atau data belum memadai. |
| `READY` | Momentum sudah terbentuk; masih menunggu konfirmasi 15M. |
| `TRIGGERED` | Kandidat lolos gerbang data dan memperoleh breakout atau retest bounce 15M. |
| `EXTENDED` | Harga terlalu jauh/overbought; menunggu pullback. |
| `FAILED` | Trigger menunjukkan breakdown. |

Hanya `TRIGGERED` dapat menghasilkan label `STRONG_BUY` atau `TRIGGERED_BUY`. Risk Guard mengubah rekomendasi menjadi `AVOID`; sentimen `BEARISH` dengan `v2_score >= 70` menghasilkan `CAUTION` sebelum pemeriksaan label beli.

Trigger 15M tidak boleh membatalkan status `EXTENDED` atau `FAILED` dari timeframe lebih tinggi. Candle 15M dengan kenaikan dari open ke close minimal 4% atau RSI minimal 78 dikategorikan `EXTENDED`, mendahului pemeriksaan breakout.

## Integritas data dan indikator

Scanner memvalidasi jendela terbaru setiap simbol:

- Struktur: 51 candle pada interval yang dipilih.
- Momentum: 35 candle 1H.
- Trigger: 21 candle 15M.

Candle wajib memiliki `is_closed=1`, metadata `open_time`, `close_time`, dan `fetched_at` lengkap, OHLCV valid, serta urutan tanpa duplikasi atau jeda dalam jendela validasi. Umur candle terakhir dan waktu pengambilannya dibatasi satu interval ditambah toleransi 60 detik. Snapshot ticker 24 jam harus diambil dalam 1 jam ditambah 60 detik.

Data struktur atau ticker tidak valid tidak menghasilkan sinyal untuk simbol tersebut. Kekurangan momentum, benchmark, atau trigger membuat kandidat `PARTIAL` dan memblokir entry baru. Diagnostik tersimpan di `backend/data/data_quality_latest.json`. History lama tetap tersimpan; metadata yang tidak diketahui tidak dianggap valid secara otomatis.

Relative strength membandingkan return koin dan BTC dengan batas candle sekarang **dan sebelumnya** yang sama persis. Benchmark yang tidak tersedia ditampilkan `N/A` dan disimpan sebagai SQL `NULL` / JSON `null`. `rs_structure` mengikuti interval pilihan; `rs_4h` hanya diisi untuk scan 4H.

RSI scanner dan trigger 15M memakai implementasi bersama di `feature_engine.py`, dengan rata-rata rolling gain/loss. Warm-up dan harga datar bernilai 50, kenaikan tanpa penurunan bernilai 100, dan penurunan tanpa kenaikan bernilai 0. Snapshot historis mempertahankan indikator saat scan dibuat.

## Trading plan dinamis

Trading plan memuat buy area, `entry_price`, stop loss, TP1, TP2, dan rasio risiko/imbalan. Entry awal memakai titik tengah buy area; konfirmasi 15M memperbarui area entry berdasarkan mikro-support dan menghitung ulang target.

Default tanpa konfigurasi kalibrasi:

| Parameter | Perhitungan |
|---|---|
| TP1 | Jarak terbesar antara 4.5% dari entry dan 1.6 × ATR. |
| TP2 | Jarak terbesar antara 7.5% dari entry dan 3.0 × ATR. |
| SL struktural | Harga lebih rendah antara support × 0.990 dan entry − 1.25 × ATR, lalu dibatasi risiko maksimum. |
| Batas risiko SL | Maksimal 4.8% dari entry; pembulatan harga menjaga batas tersebut. |

Plan awal memakai ATR 1H bila tersedia, dengan ATR struktur sebagai fallback. ATR yang dipakai tersebut disimpan sebagai `atr14` pada hasil scan dan digunakan lagi saat perhitungan ulang setelah trigger 15M. TP1/SL dan multiplier ATR dapat diubah melalui `backend/data/calibrated_config.json`; TP2 memakai rumus di atas dengan batas minimum sebesar TP1. `stop_loss_pct` konfigurasi adalah batas jarak kerugian dari entry, bukan persentase stop yang harus selalu dicapai.

## Analisis berita dan fallback

Modul berita menggabungkan kueri risiko dan kueri umum Google News RSS, memfilter kecocokan simbol/nama aset dan usia publikasi maksimal 72 jam, menghapus duplikasi judul, lalu mengambil hingga empat headline. Judul, sumber, dan tanggal publikasi disertakan dalam prompt. Respons Gemini wajib memiliki enum sentimen/dampak yang benar, skor numerik finite dalam −1 hingga 1, dan flag risiko bertipe boolean.

Model memakai `GEMINI_MODEL`, atau `gemini-flash-latest` jika tidak diatur. Evidence headline, model, hash prompt, dan waktu analisis disimpan. Status `AVAILABLE`, `NO_NEWS`, `FETCH_FAILED`, dan `MODEL_FAILED` dibedakan. Data tidak tersedia tidak diperlakukan sebagai sentimen netral atau bukti bebas risiko: poin tambahan 0 dan rekomendasi `NEWS_UNVERIFIED`.

Identitas dan `scan_time` sinyal tetap berasal dari scanner; analisis ulang berita tidak membuat order baru. Sinyal berusia lebih dari 15 menit membutuhkan scan baru sebelum mendapat label beli. Risk Guard tetap menampilkan `AVOID` ketika risiko kritis terdeteksi.

Snapshot derivatif harus berumur maksimal 1 jam ditambah 60 detik. Delta 1H/4H memakai observasi yang mendekati interval masing-masing; data yang belum tersedia tetap `null`, tanpa substitusi 1H sebagai 4H. Funding negatif saja tidak diklaim sebagai bukti short squeeze.

## Outcome tracker dan kalibrasi

Kedua modul ini dijalankan terpisah dari `main.py`:

```powershell
# Evaluasi sinyal TRIGGERED dan kumpulkan candle forward.
python -m backend.outcome_tracker

# Evaluasi ulang dengan time-stop berbeda.
python -m backend.outcome_tracker --timeout 8 --recheck

# Biaya dalam persen; gunakan --recheck bila mengubah asumsi.
python -m backend.outcome_tracker --fee-pct 0.10 --slippage-pct 0.05 --spread-pct 0.02 --recheck

# Eksperimen tanpa menyimpan konfigurasi.
python -m backend.calibration_lab

# Simpan konfigurasi yang sudah dievaluasi di luar sampel.
python -m backend.calibration_lab --apply
```

Tracker mengambil identitas dari tabel scan dan mengevaluasi hanya `TRIGGERED`. JSON berita lama tidak diimpor sebagai order. Default: candle 1H, batas menunggu fill 24 jam, dan time-stop 6 jam setelah fill. Fill memerlukan low/high mencakup entry eksplisit; candle yang sudah dimulai sebelum scan tidak dipakai. Timestamp fill/exit memiliki resolusi candle, bukan waktu transaksi aktual.

Status outcome: `PENDING`, `UNFILLED`, `TP1_HIT`, `TP2_HIT`, `SL_HIT`, `TIMEOUT`, atau `AMBIGUOUS`. Kebijakan replay keluar penuh di TP1; TP2 merupakan target informasi, kecuali sinyal lama hanya memiliki TP2. Fill dan exit pada candle yang sama, atau TP dan SL pada candle aktif yang sama, menjadi `AMBIGUOUS` dengan P&L tidak diketahui. Timeout keluar pada open candle deadline; gap di bawah SL menggunakan harga open yang lebih buruk.

Exit, return bruto/neto, serta asumsi biaya disimpan. Default fee 0.10% dan slippage 0.05% per sisi, spread penuh 0.02% (setengah spread per sisi); ini asumsi simulasi yang dapat diubah melalui CLI, bukan biaya aktual akun. MFE/MAE hanya diagnostik, bukan dasar menebak urutan TP/SL.

Cache `signal_forward_candles` tetap mengumpulkan harga setelah exit lama, sampai 31 jam dari scan pada kebijakan default. Deadline candle juga harus sudah tertutup agar dataset kalibrasi lengkap. Jendela tracker diperpanjang untuk `--timeout` yang lebih lama, sementara eksperimen kalibrasi memakai kebijakan tetap 24H menunggu fill + 6H posisi. Gap atau OHLCV tidak valid menghalangi pemakaian replay.

Calibration lab memakai candle forward lengkap, metadata scoring versi sekarang, dan sinyal `TRIGGERED` dengan kualitas data `OK`. Setiap konfigurasi diuji ulang candle demi candle dengan fungsi trading plan dan scoring yang sama dengan scanner. Data lama yang hanya memiliki MFE/MAE tidak digunakan untuk menguji parameter exit baru.

Pembagian kronologis train/validation/test memakai proporsi 60/20/20 pada kelompok timestamp; horizon label yang melintasi batas dibuang dari partisi sebelumnya. Train mengusulkan parameter dan bobot, validation memilih konfigurasi, lalu test mengevaluasi konfigurasi yang dibekukan. Tiga fold walk-forward dan perbandingan v1/v2 juga dilaporkan. Sampel setelah purge harus memenuhi ukuran minimum; history pendek dapat belum memenuhi syarat.

Simulasi portofolio mencadangkan 20% ekuitas per order dengan maksimum lima posisi/order bersamaan. Return memakai biaya dan keterbatasan modal; drawdown yang dilaporkan merupakan drawdown ekuitas terealisasi, belum mark-to-market intratrade. Outcome ambigu tidak diberi P&L buatan, dan konfigurasi dengan outcome belum pasti tidak boleh diterapkan melalui `--apply`.

**Cakupan eksperimen masih kandidat TRIGGERED yang tersimpan, tanpa skor berita historis.** Perbandingan v1/v2 menggunakan pool tersebut; belum merupakan backtest seluruh universe atau bukti efektivitas pemilihan koin yang tidak tersimpan. Konfigurasi disimpan persis seperti yang diuji, dengan versi scoring, biaya, dan cakupan evaluasi. Mulai proses scanner baru setelah menerapkan konfigurasi.

Kelulusan tes perangkat lunak dan hasil sintetis bukan bukti profitabilitas. Validasi strategi membutuhkan history forward baru, sampel memadai, dan evaluasi di luar sampel; belum ada klaim signifikansi statistik.

## Struktur repo dan output

```text
KripikTo/
├── .agents/MDFile/KRIPIKTO_V2_6_PHASES.md
├── backend/
│   ├── data/                         # Dibuat saat modul dijalankan
│   │   ├── kripto.db
│   │   ├── scan_latest.json
│   │   ├── data_quality_latest.json
│   │   ├── final_recommendations.json
│   │   └── calibrated_config.json    # Dibuat dengan calibration_lab --apply
│   ├── data_pipeline.py
│   ├── data_integrity.py
│   ├── feature_engine.py
│   ├── scanner.py
│   ├── macro_sentiment.py
│   ├── derivatives_flow.py
│   ├── news_sentiment.py
│   ├── outcome_tracker.py
│   ├── research_utils.py
│   ├── calibration_lab.py
│   └── __init__.py
├── tests/
├── main.py
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

SQLite menyimpan tabel `market_summary_24h`, `klines_history`, `macro_sentiment`, `derivatives_summary`, `derivatives_history`, `scan_results`, `kripto_sentiment_analysis`, `signal_outcomes`, dan `signal_forward_candles`. JSON `scan_latest` dan `final_recommendations` adalah hasil terbaru, sedangkan database menyimpan snapshot historis. Tahap berita mengosongkan JSON rekomendasi final ketika API key atau kandidat tidak tersedia. History versi lama tetap disimpan, tetapi tidak otomatis dianggap valid untuk kalibrasi baru.

## Tes regresi

```powershell
python -m unittest discover -s tests -v
```

Tes mencakup RSI, gerbang entry, batas risiko, freshness, schema Gemini, identitas sinyal, urutan TP/SL, biaya dan modal terbatas, cache forward, purge horizon, serta alur kalibrasi lengkap pada data sintetis. Integrasi menggunakan fixture dan mock jaringan.
