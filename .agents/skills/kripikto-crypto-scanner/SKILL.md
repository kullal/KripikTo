---
name: kripikto-crypto-scanner
description: >-
  Panduan operasional, arsitektur data, dan strategi trading kuantitatif untuk sistem KripikTo.
  Gunakan skill ini saat menjalankan, mengembangkan, memodifikasi, atau men-debug pipeline data kripto,
  pemindai teknikal & whale flow, analisis sentimen berita Google Gemini AI, serta strategi spot scalping Binance.
---

# KripikTo: Quantitative Crypto Scanner & AI Sentiment System

KripikTo adalah sistem pemindai pasar kripto terpadu yang menggabungkan analisis kuantitatif (*on-chain whale flow*, indikator teknikal, pendanaan derivatif) dengan analisis kualitatif fundamental (*Google News RSS* + *Google Gemini Flash LLM Risk Guard*).

---

## 🏗️ Alur Arsitektur 4 Tahap

1. **Tahap 0: Barometer Sentimen Makro (`backend/macro_sentiment.py`)**
   - Mengambil data *Crypto Fear & Greed Index* (Alternative.me).
   - Menentukan rezim pasar: *Extreme Fear, Fear, Neutral, Greed, Extreme Greed*.
   - Mengatur sensitivitas skor teknikal secara adaptif (misal: penalti false breakout saat Extreme Fear, deteksi akumulasi cerdas saat Greed).

2. **Tahap 1: Pipa Data Pasar Spot & Filter Status Listing (`backend/data_pipeline.py`)**
   - Sumber: *Binance Public Vision API* (`https://data-api.binance.vision`) bebas akses tanpa VPN/API key.
   - Mengunduh ringkasan pasar 24 jam dan 100 candle (klines) untuk Top 500 koin likuid USDT.
   - **Filter Wajib**:
     - Memeriksa endpoint `exchangeInfo` untuk memastikan koin berstatus **`TRADING`** dan **`isSpotTradingAllowed = True`** (menyingkirkan koin delisted/halted/break).
     - Memfilter seluruh stablecoin, token fiat, dan *dynamic dollar-peg* (`is_stablecoin_or_excluded`).

3. **Tahap 2: Pemindai Kuantitatif, Aliran Paus & Derivatif (`backend/scanner.py`)**
   - **Whale Flow**: Mengukur *Taker Buy Ratio* (persentase beli hajar kanan paus).
   - **Derivatif**: Mengambil *Funding Rate* Binance Futures via CoinGecko publik untuk mendeteksi potensi *Short Squeeze* (FR < -0.015%).
   - **Teknikal**: MA20, MA50, Breakout 20-bar High, Volume Spike (>1.4x), RSI 14, dan ATR 14.
   - **Trading Plan Anti-Beli Pucuk**:
     - Mendeteksi *overextension* (RSI tinggi atau harga menjauh dari MA20).
     - Menghitung `buy_area` di area **Support / Retest** (bukan di harga pucuk candle running).
     - Stop Loss adaptif berbasis struktur support dan toleransi ATR (-4.0% s.d -5.8%).
     - Target Profit utama (+6.0%) dihitung dari harga beli antrean diskon.

4. **Tahap 3: Analisis Sentimen Berita Global & Crypto Risk Guard (`backend/news_sentiment.py`)**
   - Mengambil headline berita terkini per koin dari Google News RSS (CoinDesk, Cointelegraph, Decrypt, dll).
   - Dianalisis oleh Google Gemini Flash LLM dengan ekstraksi katalis dalam Bahasa Indonesia.
   - **Crypto Risk Guard**: Jika terdeteksi risiko fatal (Peretasan / *Exploit*, Gugatan Regulasi SEC/CFTC, Delisting, Token Dumping), skor otomatis dipangkas maksimal ke angka 35 dan diberi status **`⚠️ AVOID`**.

---

## 💻 Panduan Eksekusi Terminal

Jalankan perintah menggunakan virtual environment (`.venv`):

```powershell
# Jalankan alur penuh (Download -> Scan -> Analisis AI):
.\.venv\Scripts\python.exe main.py

# Mode Cepat (Melewati unduhan jika data baru saja diunduh hari ini):
.\.venv\Scripts\python.exe main.py --skip-download

# Mengubah Timeframe Candlestick (1h, 2h default, 4h, 1d):
.\.venv\Scripts\python.exe main.py --interval 4h

# Membatasi jumlah koin teratas yang dianalisis oleh AI:
.\.venv\Scripts\python.exe main.py --top 10
```

---

## 🎯 Panduan Eksekusi Order di Binance Spot

1. **Jika Koin Belum Terbeli (Entry Baru):**
   - Gunakan **Limit Order** dengan mencentang kotak **`[✓] TP/SL`**.
   - Masukkan harga di dalam rentang `buy_area` hasil scan.
   - Isi `TP Limit` sesuai rekomendasi bot (+6.0%).
   - Isi `SL Trigger` dan `SL Limit` sesuai toleransi proteksi modal.

2. **Jika Koin Sudah Terbeli di Dompet Spot (Pasang Ulang TP/SL):**
   - Pilih menu **`OCO`** (One-Cancels-the-Other) di tab Jual (Sell):
     - **Price**: Target Take Profit.
     - **Stop**: Harga Pemicu Stop Loss (Trigger).
     - **Limit**: Harga Jual Batas Terendah (Limit).
     - **Amount**: 100% koin.
