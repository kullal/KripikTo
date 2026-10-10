# Ringkasan sesi KripikTo

Terakhir diperbarui: 2026-10-09, zona pengguna Asia/Jakarta.
Workspace: `D:\Repo\KripikTo`, shell PowerShell.
Branch saat diperiksa: `kripiktoV4A`.
Commit implementasi terakhir: `ec2f9d3` (`feat: 3 features`).
Pengguna melakukan commit dan push sendiri. Sebelum penambahan dokumen ini,
working tree bersih. Jangan menganggap kondisi Git ini tetap berlaku.

## Tujuan dan keputusan pengguna

Pengguna meminta review codebase dan riset strategi, lalu menyetujui pengerjaan:
perbaiki replay outcome dan konsistensi scoring, kemudian bandingkan breakout
langsung dengan breakout-retest setelah biaya. Implementasi sudah dibuat.

Pengguna juga meminta toggle news. `main.run_all(news_enabled=False)` dan
CLI `--no-news` sudah tersedia. Default tetap `news_enabled=True`.
Tidak ada kesimpulan bahwa salah satu strategi pasti paling menguntungkan.

Permintaan terakhir sebelum dokumentasi ini adalah menjelaskan mengapa ada
tiga perintah. Penjelasan sudah diberikan; belum ada permintaan untuk
menggabungkan perintah atau membuat scheduler. Pengguna sekarang meminta
ringkasan konteks disimpan agar dapat dibaca pada chat baru.

## Alur sekarang dan alasan tiga perintah

1. `main.py --no-news`: mengambil data publik Binance, memeriksa kualitas,
   menilai struktur 4H, momentum 1H, dan trigger 15M. Menyimpan snapshot sinyal
   dan trading plan di SQLite serta `backend/data/scan_latest.json`.
   Mode tanpa news melewati RSS/Gemini. `final_recommendations.json` tidak
   diperbarui, sehingga isinya bisa berasal dari scan lama dengan news.
   Fear & Greed makro tetap diambil pada mode tanpa news.
2. `backend.outcome_tracker`: dijalankan lagi setelah harga berikutnya tersedia.
   Mengambil candle setelah waktu sinyal, menyimpan cache `replay_candles`,
   menyimulasikan fill, TP1, SL, atau time stop, lalu menghitung hasil setelah
   fee/slippage. Menyimpan outcome di SQLite. Bukan order sungguhan.
3. `backend.strategy_comparison`: memakai snapshot breakout dan cache candle
   dari tracker untuk membandingkan entry langsung dengan entry setelah retest
   terkonfirmasi. Database dibaca saja, laporan ditulis ke
   `backend/data/strategy_comparison.json`. Tidak mengunduh candle sendiri.

Main saja cukup untuk mencari kandidat sekarang. Dua modul tambahan diperlukan
untuk mengukur hasil dan membandingkan strategi secara empiris. Tidak perlu
menunggu berjam-jam dalam satu proses: tracker bisa dijalankan berkala untuk
sinyal lama, lalu laporan perbandingan diperbarui. Laporan lengkap membutuhkan
forward window yang sudah tersedia; menjalankan tiga perintah langsung setelah
scan tidak menciptakan data masa depan.

## Implementasi penting per file

- `backend/scoring.py`: kontrak scoring bersama, versi
  `v3-normalized-replay`. Batas raw pilar: structure 25, momentum 30, flow 20,
  derivatives 15; bobot baseline sama dan news 10. Kontribusi dihitung sebagai
  raw / batas raw * bobot, bukan memotong raw dengan bobot baru. Makro adalah
  modifier `macro_score - 5`; penalti eksplisit; skor teknikal dibatasi 0..90.
- `backend/scanner.py`: memakai scoring bersama dan menyimpan versi, raw,
  bobot, komponen, penalti, support, trigger type, dan breakout level sebagai
  snapshot. Skor pecahan dipertahankan. `EXTENDED`/`FAILED` timeframe lebih
  tinggi tetap dapat memblokir breakout 15M. Migrasi kolom bersifat aditif.
- `backend/feature_engine.py`: retest memerlukan breakout sebelumnya terhadap
  resistance yang dibekukan, lalu candle retest menyentuh level dan menutup
  bullish di atasnya. Dekat MA20 saja bukan retest. Konfigurasi kalibrasi lama
  diabaikan jika versi/validasinya tidak cocok. TP/SL live dan kalibrasi memakai
  fungsi yang sama; support snapshot disesuaikan dengan support entry mikro.
- `backend/data_integrity.py`: minimum candle trigger 29, structure 51,
  momentum 35; scanner menggunakan candle tertutup dan validasi metadata.
- `backend/replay.py`: mesin murni, versi `v3-ohlc-costs`; validasi OHLC,
  urutan, interval, kelengkapan, dan waktu candle. Tidak menggunakan aggregate
  MFE/MAE untuk menebak urutan TP/SL. Naive timestamp diperlakukan sebagai UTC.
- `backend/replay_store.py`: cache candle forward per symbol/interval/open_time.
- `backend/outcome_tracker.py`: mengimpor snapshot scan, mengevaluasi sinyal
  `TRIGGERED` + kualitas `OK` yang tidak eksplisit diblokir execution eligibility.
  Riwayat legacy dipisahkan dari statistik replay baru. Hasil dikelompokkan
  berdasarkan interval, biaya, dan jendela waktu. Cache forward tetap diperbarui
  sampai kebutuhan observasi 48 jam terpenuhi, walau trade sudah selesai.
  `--recheck` mengevaluasi ulang riwayat, bukan wajib untuk setiap run.
- `backend/strategy_comparison.py`: hanya snapshot scoring baru,
  `TRIGGERED` + `OK` + trigger `BREAKOUT`, tanpa blok eligibility eksplisit.
  Kedua arm memakai window forward yang sama, SL/TP absolut yang sama,
  serta asumsi biaya dan lama hold yang sama. Entry retest di open candle
  sesudah candle konfirmasi; entry langsung di open candle pertama setelah scan.
  Window bersama harus lengkap. Tidak memilih pemenang otomatis.
- `backend/calibration_lab.py`: dataset snapshot versi baru dan jalur candle;
  train/test berdasarkan kelompok timestamp scan, split 70/30 dengan purge
  8,25 jam. Minimum 20 train dan 10 test. Run utama menala exit dengan bobot
  baseline tetap. Simulasi dari MFE/MAE agregat ditolak. `--apply` memerlukan
  minimal 10 outcome test selesai, versi replay sesuai, PF finite > 1 dan
  expectancy net > 0; kegagalan validasi tidak menimpa konfigurasi lama.
- `backend/news_sentiment.py`: pembacaan skor diubah menjadi float agar skor
  pecahan tidak hilang. Seluruh perilaku fallback news belum diperbaiki/ditinjau
  ulang dalam implementasi replay ini.
- `README.md`: alur tanpa news, kontrak v3, asumsi, command, dan keterbatasan
  replay/calibration telah dijelaskan.

## Asumsi replay dan batas hasil

- Default candle 15M, expiry entry limit 2 jam, holding time stop 6 jam.
- Fee default 10 bps per sisi (0,1%); slippage default 5 bps (0,05%) diterapkan
  pada eksekusi market. Ini asumsi eksperimen, bukan tarif akun pengguna.
- Fill limit tidak diberi harga lebih buruk daripada limit. TP1 menutup seluruh
  posisi; TP2/partial exit belum disimulasikan.
- Candle yang mengandung scan dikeluarkan; replay mulai pada boundary berikutnya.
- Fill limit dan exit pada candle sama, atau TP/SL tersentuh bersama tanpa
  urutan yang diketahui, dapat menghasilkan `AMBIGUOUS` tanpa PnL buatan.
- Gap, data tidak lengkap, dan expiry/deadline di dalam candle ditangani
  konservatif. Time stop memakai open pada deadline, bukan high/low sesudahnya.
- Return observasi 12/24/48 jam terpisah dari hasil trade.
- Total return adalah penjumlahan hasil trade, bukan return portofolio. Belum ada
  model modal, posisi tumpang tindih, equity/drawdown portofolio, antrean fill,
  order book, atau partial fill. Single holdout belum membuktikan strategi robust.

## Validasi yang sudah dilakukan

Pada sesi implementasi sebelumnya, seluruh **81 test unittest lulus** dan
`git diff --check` bersih. Ini hasil validasi historis sesi tersebut, bukan klaim
bahwa test dijalankan ulang saat menulis ringkasan ini. Test mencakup replay
berurutan, biaya, gap, ambigu, invalid data, expiry, timeout, scoring bersama,
retest, pairing, cache, eligibility, dan guard kalibrasi.

Environment `venv` dibuat dan dependencies requirements dipasang. `.venv`
lama sebelumnya menunjuk interpreter di profil pengguna ACER yang tidak ada.
Gunakan `.\venv\Scripts\python.exe`; cek ulang environment jika mesin berubah.

## Kondisi data terakhir yang diverifikasi

`scan_latest.json` dibaca saat dokumentasi ini dibuat:

- `scan_time`: `2026-10-09 16:11:30` (UTC dalam snapshot).
- 9 kandidat: 2 `WAIT`, 7 `EXTENDED`, seluruhnya kualitas `OK`.
- Tidak ada `TRIGGERED` pada scan ini; belum ada entry eligible baru untuk
  replay/perbandingan dari scan tersebut. Breakout mikro tetap dapat diblokir
  oleh `EXTENDED` timeframe lebih tinggi.
- Laporan perbandingan yang tersimpan: 0 pasangan, 224 snapshot legacy dilewati.
  Laporan itu berasal dari run sebelumnya dan tidak otomatis diperbarui oleh main.
- Quality report terakhir `checked_at`: `2026-10-09T16:11:24.329000+00:00`.
  Banyak warning historis bukan berarti unduhan top 200 semuanya gagal: scanner
  juga memeriksa symbol lama yang masih berada di SQLite.

## Hal yang masih terbuka

1. Pipeline pernah melewati boundary candle: BTC diambil sebelum boundary,
   altcoin sesudahnya, sehingga `BTC_ALIGNMENT_MISSING` dan `PARTIAL` muncul.
   Run berikutnya menghasilkan semua kandidat `OK`. Belum ada perbaikan khusus
   common cutoff/sinkronisasi waktu untuk kasus ini pada sesi implementasi.
2. Belum cukup snapshot baru yang eligible dengan window forward lengkap untuk
   menyimpulkan breakout langsung atau retest lebih baik. Jangan memakai hasil
   legacy atau sinyal WAIT/EXTENDED sebagai trade eligible untuk mengisi sampel.
3. News fallback/risk guard dan kontrak timestamp snapshot news masih perlu
   audit bila pengguna ingin memakai mode news lagi. `--no-news` tidak memperbarui
   final recommendations lama dan tidak berarti risiko berita terverifikasi.
4. Integrasi otomatis tracker/comparison ke main atau scheduler belum dibuat.
   Jika nanti diminta, hindari menunggu 8 jam secara blocking; evaluasi sinyal
   historis yang datanya sudah tersedia dan laporkan pending secara jelas.
5. Kalibrasi lanjut (walk-forward berulang, risiko overfitting, model portofolio)
   belum diterapkan. Pertahankan prinsip measurement before optimization.

## Referensi riset dari sesi sebelumnya

Referensi ini mendasari ide pengujian, bukan bukti bahwa strategi tertentu cocok
untuk KripikTo. Periksa sumber terbaru lagi bila memberi rekomendasi baru.

- [Yale: risiko dan momentum cryptocurrency](https://economics.yale.edu/news/180806/assessing-cryptocurrency-yale-economist-aleh-tsyvinski)
- [Common Risk Factors in Cryptocurrency](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3379131)
- [The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)
- [Binance Spot market data](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market)
- [Binance Spot filters](https://developers.binance.com/en/docs/products/spot/filters)
