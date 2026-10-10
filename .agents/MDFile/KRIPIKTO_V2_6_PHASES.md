# KripikTo v2 — 6 Phase Implementation Specification

## Tujuan Dokumen

Dokumen ini menjadi spesifikasi kerja untuk AI Agent yang mengembangkan **KripikTo v2**, sistem scanner kripto berbasis quantitative market analysis, multi timeframe analysis, derivatives, market benchmark, news sentiment, outcome tracking, dan empirical calibration.

Tujuan utama v2 bukan menambah indikator sebanyak mungkin. Tujuan utamanya adalah:

> **Measurement before optimization.**

Setiap perubahan harus dapat diukur, direproduksi, dan dibandingkan dengan versi sebelumnya.

Sistem v2 harus mampu membedakan:

```text
ACCUMULATION
MOMENTUM FORMATION
ENTRY TRIGGER
EXTENDED / DO NOT CHASE

NO SETUP
```

Jangan menyamakan skor kondisi tinggi dengan probabilitas harga langsung naik.

---

# Prinsip Arsitektur

## Pembagian Timeframe

```text
4H  = Structure / Regime
1H  = Momentum / Kinetic Energy
15M = Tactical Entry Trigger
```

Jangan menggunakan timeframe untuk menjawab pertanyaan yang bukan tanggung jawabnya.

Contoh:

```text
4H bullish
```

tidak otomatis berarti:

```text
BUY
```

Dan:

```text
15M breakout
```

tidak otomatis berarti:

```text
coin bagus
```

15M hanya mengonfirmasi timing entry dari kandidat yang sudah lolos timeframe lebih tinggi.

---

# Prinsip Data Integrity

Semua data historis yang digunakan untuk scanner harus bebas lookahead bias.

Setiap candle wajib memiliki:

```text
symbol
interval
open_time
close_time
is_closed
fetched_at
```

Scanner struktural hanya membaca:

```sql
is_closed = 1
```

Candle yang masih berjalan boleh disimpan dengan:

```text
is_closed = 0
```

tetapi tidak boleh digunakan untuk perhitungan indikator historis.

---

# Prinsip Measurement

Semua sinyal v2 harus memiliki snapshot.

Minimal snapshot menyimpan:

```text
signal_id
scan_time
symbol
interval
candle_open_time
candle_close_time

structure_score
momentum_score
flow_score
derivatives_score
news_score

setup_type
entry_status

entry_price
buy_low
buy_high
stop_loss
tp1
tp2
atr14

v1_score
v2_score
```

Outcome harus dapat dilacak setelah sinyal dibuat.

---

# Prinsip Eksperimen

Threshold dan bobot bukan dogma.

Contoh baseline:

```python
WEIGHTS = {
    "structure": 25,
    "momentum": 30,
    "flow": 20,
    "derivatives": 15,
    "news": 10,
}
```

Nilai di atas hanya **baseline experiment**.

Jangan mengklaim bobot tersebut optimal sebelum diuji pada data historis dan out-of-sample.

---

# PHASE 1 — Data Integrity Layer & Closed Candle Fix

## Tujuan

Membersihkan fondasi data agar semua perhitungan berikutnya menggunakan candle yang benar dan tidak terkena lookahead bias.

## File utama

```text
backend/data_pipeline.py
backend/scanner.py
```

Jika perlu tambahkan helper:

```text
backend/data_integrity.py
```

Tidak wajib membuat file baru jika implementasi sederhana dapat dipertahankan pada modul yang sudah ada.

---

## 1.1 Schema `klines_history`

Pastikan terdapat:

```text
symbol
interval
open_time
close_time
is_closed
fetched_at
open
high
low
close
volume
quote_volume
taker_buy_volume
```

Tipe yang disarankan:

```text
close_time  INTEGER
is_closed   INTEGER
fetched_at  TEXT
```

`is_closed` menggunakan:

```text
1 = closed
0 = forming
```

Tambahkan index untuk query closed candle:

```sql
CREATE INDEX IF NOT EXISTS idx_klines_closed
ON klines_history(symbol, interval, is_closed, open_time DESC);
```

---

## 1.2 Closed Candle Detection

Saat Binance mengembalikan kline:

```text
open_time
close_time
...
```

tentukan:

```python
is_closed = 1 if close_time <= now_ms else 0
```

Jangan menghapus candle forming secara permanen.

Simpan:

```text
is_closed = 0
```

agar data real time tetap tersedia untuk future alerting.

---

## 1.3 Scanner Filter

Scanner struktural hanya menggunakan:

```sql
WHERE interval = ?
AND is_closed = 1
```

Jangan menggunakan candle forming untuk:

```text
MA
EMA
RSI
ATR
Volume Ratio
Breakout
ROC
Momentum
Scoring
Backtest
```

---

## 1.4 Volume Bug Fix

Volume ratio harus membandingkan:

```text
closed candle volume
/
MA20 volume dari candle yang sejenis
```

Contoh:

```python
vol_ratio = volume_candle / vol_ma20
```

Bukan:

```text
24H volume / 4H MA20
```

Pastikan nama kolom tidak membingungkan setelah merge DataFrame.

Gunakan naming jelas:

```text
volume_candle
quote_volume_candle
vol_ma20
```

---

## Acceptance Criteria

Phase 1 dianggap selesai apabila:

```text
[ ] closed candle dapat dibedakan dari forming candle
[ ] scanner tidak menggunakan forming candle
[ ] close_time tersimpan
[ ] fetched_at tersimpan
[ ] index closed candle tersedia
[ ] volume ratio membandingkan candle vs candle MA
[ ] database lama dapat dimigrasikan tanpa kehilangan data
[ ] compile / import test PASS
```

---

# PHASE 2 — Multi Timeframe Pipeline + BTC Benchmark

## Tujuan

Mengubah scanner dari single timeframe menjadi funnel:

```text
4H → 1H → 15M
```

dengan BTC sebagai benchmark market.

---

# 2.1 Universe

Gunakan universe awal:

```text
Top N liquid USDT pairs
```

Contoh baseline:

```text
Top 500
```

Tetapi BTCUSDT harus diperlakukan sebagai benchmark khusus.

Jangan bergantung pada apakah BTC masuk Top N.

Universe logis:

```text
Eligible Altcoins
+
BTCUSDT benchmark
```

---

# 2.2 Data 4H

4H dipakai untuk:

```text
trend
structure
EMA20
EMA50
RSI
ATR
breakout structure
relative strength vs BTC
```

Output tahap ini harus dapat menyaring universe menjadi kandidat lebih kecil.

Contoh:

```text
500
↓
4H filter
↓
50–100 kandidat
```

Angka tersebut adalah target operasional, bukan syarat matematis tetap.

---

# 2.3 Data 1H

1H dipakai untuk:

```text
ROC
Price acceleration
ATR expansion
Volume expansion
RS vs BTC
Open Interest delta
Funding delta
```

Tahap ini menyaring kandidat lagi.

Contoh:

```text
50–100
↓
1H momentum
↓
10–30 kandidat
```

---

# 2.4 Data 15M

15M hanya digunakan pada kandidat yang telah lolos.

Deteksi:

```text
breakout
retest
support reclaim
swing high break
volume confirmation
```

Jangan menggunakan 15M untuk menentukan fundamental quality atau long term trend.

---

# 2.5 BTC Benchmark

Simpan BTC:

```text
BTC 4H
BTC 1H
```

Return coin dan return BTC wajib menggunakan candle yang sama periodenya.

Formula:

```text
RS_4H = coin_return_4H - btc_return_4H
RS_1H = coin_return_1H - btc_return_1H
```

Pastikan alignment menggunakan:

```text
open_time
close_time
```

bukan sekadar baris terakhir DataFrame.

Jika candle coin dan BTC tidak memiliki timestamp yang cocok:

```text
jangan hitung RS secara diam-diam
```

Tangani missing alignment secara eksplisit.

---

## Acceptance Criteria

```text
[ ] 4H tersedia
[ ] 1H tersedia
[ ] 15M tersedia untuk kandidat
[ ] BTCUSDT 4H tersedia
[ ] BTCUSDT 1H tersedia
[ ] RS_4H tersedia
[ ] RS_1H tersedia
[ ] coin/BTC candle alignment tervalidasi
[ ] pipeline tidak mengunduh 15M seluruh universe jika tidak diperlukan
[ ] tidak ada lookahead
```

---

# PHASE 3 — Outcome Tracker Engine

## Tujuan

Membangun mesin pengukuran hasil sinyal.

Outcome tracker bukan fitur tambahan belakangan. Ia adalah alat validasi utama sistem.

---

# 3.1 Tabel `signal_outcomes`

Minimal:

```text
signal_id
symbol
created_at

entry_price
buy_low
buy_high
stop_loss
tp1
tp2
atr14

is_filled
fill_time
fill_price

mfe_pct
mae_pct

tp1_hit
tp2_hit
sl_hit

result
duration_hours
timeout

exit_price
exit_time
```

Tambahkan:

```text
v1_score
v2_score
setup_type
entry_status
```

untuk A/B comparison.

---

# 3.2 Definisi `is_filled`

Jika buy area:

```text
buy_low → buy_high
```

maka sinyal dianggap filled jika market price menyentuh area tersebut setelah waktu sinyal dibuat.

Jika tidak pernah menyentuh:

```text
is_filled = 0
```

Sinyal unfilled jangan dihitung sebagai win/loss trade.

---

# 3.3 MFE

Maximum Favorable Excursion.

Contoh:

```text
Entry = 0.260
Maximum favorable price = 0.278
```

MFE:

```text
(0.278 - 0.260) / 0.260 * 100
```

Simpan sebagai:

```text
mfe_pct
```

---

# 3.4 MAE

Maximum Adverse Excursion.

Contoh:

```text
Entry = 0.260
Lowest price while active = 0.255
```

MAE:

```text
(0.255 - 0.260) / 0.260 * 100
```

Simpan sebagai:

```text
mae_pct
```

---

# 3.5 Ambiguous Same-Candle Event

OHLC tidak selalu dapat menentukan urutan TP dan SL.

Contoh:

```text
high >= TP1
dan
low <= SL
```

dalam candle yang sama.

Jangan mengarang urutan.

Pilihan yang diperbolehkan:

```text
AMBIGUOUS
```

atau evaluasi menggunakan timeframe yang lebih kecil jika data tersedia.

Jangan otomatis menganggap TP hit terlebih dahulu.

---

# 3.6 Time Stop

Baseline awal:

```text
6 jam
```

untuk strategi momentum.

Dapat disimpan sebagai parameter:

```python
TIME_STOP_HOURS = 6
```

Jangan menggunakan 48 jam sebagai default untuk setup yang didefinisikan sebagai momentum/scalping.

Alternatif evaluasi tambahan dapat berupa:

```text
12H
24H
48H
```

tetapi harus dibedakan dari strategi timeout utama.

---

# 3.7 A/B Snapshot v1 vs v2

Pada snapshot yang sama simpan:

```text
v1_score
v1_recommendation
v2_score
v2_setup_type
v2_entry_status
```

Tujuannya:

```text
apakah v2 benar-benar menyaring sinyal sideways lebih baik daripada v1?
```

---

## Acceptance Criteria

```text
[ ] semua sinyal tercatat
[ ] unfilled signal dapat dibedakan
[ ] MFE dihitung
[ ] MAE dihitung
[ ] TP1/TP2/SL dapat dicatat
[ ] timeout dapat dicatat
[ ] ambiguous event tidak diberi outcome palsu
[ ] v1 dan v2 tersimpan dalam snapshot
[ ] outcome tidak membaca data sebelum signal_time
```

---

# PHASE 4 — Feature Engine: RS, ROC, ATR, Flow Delta

## Tujuan

Membuat feature engine terpisah dari decision engine.

Feature mentah harus tersedia sebelum scoring.

Jangan mencampur:

```text
data calculation
```

dengan:

```text
business decision
```

secara berlebihan.

---

## 4.1 Structure Features — 4H

Minimal:

```text
EMA20
EMA50
RSI14
ATR14
distance_to_20bar_high
distance_to_20bar_low
RS_4H
```

Contoh alignment:

```text
close > EMA20 > EMA50
```

---

## 4.2 Momentum Features — 1H

Minimal:

```text
ROC_1H
ROC_previous_1H
price_acceleration
ATR_1H
ATR_SMA20
ATR_expansion_ratio
volume_1H
volume_MA20_1H
volume_ratio_1H
RS_1H
```

Formula:

```text
price_acceleration =
ROC_1H_current - ROC_1H_previous
```

Volatility expansion:

```text
ATR_expansion_ratio =
ATR_1H / SMA(ATR_1H, 20)
```

---

# 4.3 Derivatives Time Series

Jangan hanya menyimpan latest value.

Minimal table:

```text
derivatives_history
```

dengan:

```text
symbol
timestamp
funding_rate
open_interest
```

Index:

```text
(symbol, timestamp DESC)
```

Dari sini hitung:

```text
delta_oi_1h
delta_oi_4h
delta_funding_1h
```

Contoh:

```text
delta_oi_1h =
OI_current - OI_1h_ago
```

```text
delta_funding_1h =
FR_current - FR_1h_ago
```

---

# 4.4 Flow

Minimal:

```text
taker_buy_ratio_1H
taker_buy_ratio_24H
volume_to_market_cap
```

`volume_to_market_cap` harus disimpan sebagai continuous raw feature.

Jangan langsung mengunci:

```text
>= 0.8
```

Threshold akan dikalibrasi kemudian.

---

# 4.5 No Hidden Lookahead

Semua feature pada:

```text
signal_time
```

hanya boleh menggunakan data:

```text
timestamp <= signal_time
```

Tidak boleh mengambil candle masa depan.

---

## Acceptance Criteria

```text
[ ] RS_4H
[ ] RS_1H
[ ] ROC_1H
[ ] price_acceleration
[ ] ATR expansion
[ ] volume expansion
[ ] delta OI
[ ] delta Funding
[ ] raw volume_to_market_cap
[ ] semua feature timestamp aligned
[ ] tidak ada future data
```

---

# PHASE 5 — State Classification & Tactical Trigger

## Tujuan

Mengubah feature menjadi state market yang jujur.

Jangan memaksa setiap kandidat menjadi:

```text
BUY_MOMENTUM
```

---

# 5.1 Score Decomposition

Gunakan lima subscore:

```text
Structure     25
Momentum      30
Flow          20
Derivatives   15
AI News       10
----------------
Total         100
```

Baseline tersebut adalah eksperimen.

Buat configurable:

```python
WEIGHTS = {
    "structure": 25,
    "momentum": 30,
    "flow": 20,
    "derivatives": 15,
    "news": 10,
}
```

---

# 5.2 Structure Score

Metrik:

```text
4H EMA alignment
RS_4H
20-bar breakout position
```

Contoh komponen:

```text
close > EMA20
EMA20 > EMA50
RS_4H > 0
near breakout / breakout
```

Jangan mengklaim threshold sebagai optimal sebelum calibration.

---

# 5.3 Momentum Score

Metrik:

```text
ROC_1H
price acceleration
ATR expansion
1H volume surge
RS_1H
```

---

# 5.4 Flow Score

Metrik:

```text
taker buy ratio
1H flow
24H flow
volume participation
volume_to_market_cap
```

---

# 5.5 Derivatives Score

Metrik:

```text
funding rate
funding delta
OI delta
OI level
```

Jangan menyimpulkan short squeeze hanya dari funding rate tunggal.

---

# 5.6 AI News Score

AI News adalah salah satu input.

Jangan membuat arsitektur:

```text
70% technical
30% news
```

jika desain final menggunakan:

```text
25 + 30 + 20 + 15 + 10
```

Harus ada satu sistem final yang konsisten.

Risk Guard tetap boleh menjadi hard safety override.

Contoh:

```text
crypto_risk = true
→ final score capped
→ status = RISK_BLOCKED
```

---

# 5.7 Setup Type

Gunakan:

```text
MOMENTUM_RUNNER
ACCUMULATION_COIL
VOLATILITY_SQUEEZE
STRUCTURE_BULLISH
NO_SETUP
```

Definisi baseline:

### MOMENTUM_RUNNER

Kandidat dengan:

```text
strong structure
strong momentum
supporting flow
```

Contoh baseline:

```text
structure >= 18
momentum >= 22
flow >= 14
```

### ACCUMULATION_COIL

Kondisi:

```text
structure kuat
flow kuat
momentum belum berkembang
```

Contoh baseline:

```text
structure >= 18
flow >= 14
momentum < 12
```

### VOLATILITY_SQUEEZE

Minimal:

```text
ATR expansion rendah
volume relatif kering
```

Jangan memberi label squeeze hanya dari ATR tanpa volume condition.

### NO_SETUP

Jika tidak ada klasifikasi bermakna.

---

# 5.8 Entry Status

Gunakan:

```text
WAIT
READY
TRIGGERED
EXTENDED
FAILED
```

### WAIT

Structure/flow dapat menarik tetapi momentum belum cukup.

Contoh:

```text
ACCUMULATION_COIL
→ WAIT
```

### READY

1H momentum sudah memenuhi syarat tetapi 15M belum trigger.

### TRIGGERED

15M breakout/retest valid.

### EXTENDED

Harga telah bergerak terlalu jauh dari base/support tanpa retest.

Baseline awal:

```text
+4% sampai +6%
```

Threshold harus dapat dikalibrasi.

### FAILED

Setup invalidated.

---

# 5.9 State Transition

Sistem harus mampu menggambarkan:

```text
ACCUMULATION_COIL
        ↓
MOMENTUM_FORMING
        ↓
READY
        ↓
TRIGGERED
```

dan:

```text
ACCUMULATION_COIL
        ↓
NO_SETUP / FAILED
```

Jangan memaksa transition.

---

# 5.10 15M Trigger

15M hanya mencari:

```text
swing high breakout
volume confirmation
retest
support reclaim
```

Contoh:

```text
resistance = X

15M close > X
+
volume ratio tinggi
+
retest X berhasil
```

→ `TRIGGERED`

Jika harga sudah naik jauh tanpa retest:

```text
EXTENDED
```

---

## Acceptance Criteria

```text
[ ] five subscore terpisah
[ ] setup_type tersedia
[ ] entry_status tersedia
[ ] Accumulation ≠ Momentum
[ ] WAIT ≠ BUY
[ ] READY ≠ TRIGGERED
[ ] EXTENDED mencegah chasing
[ ] 15M hanya menjadi trigger
[ ] AI News tidak menggunakan scoring architecture ganda
```

---

# PHASE 6 — Empirical Calibration, A/B Test, dan Validation

## Tujuan

Menjawab pertanyaan:

> Apakah KripikTo v2 benar-benar lebih baik daripada v1?

Bukan:

> Apakah v2 terlihat lebih canggih?

---

# 6.1 Jangan menggunakan satu dataset untuk semua tahap

Time series tidak boleh diacak seperti dataset klasifikasi biasa.

Gunakan:

```text
TRAIN
↓
Calibration
↓
VALIDATION
↓
TEST
```

atau:

```text
Walk Forward Evaluation
```

Contoh:

```text
Train: Jan–Jun
Validation: Jul
Test: Aug

Train: Feb–Jul
Validation: Aug
Test: Sep
```

Parameter hasil train tidak boleh menggunakan informasi dari test.

---

# 6.2 Calibration Target

Threshold yang dapat diuji:

```text
RS threshold
ROC threshold
ATR expansion
volume ratio
funding delta
OI delta
extended distance
TP multiplier
SL multiplier
time stop
```

---

# 6.3 Dynamic ATR TP/SL

Baseline:

```text
SL = Entry - 1.0–1.2 × ATR_1H
TP1 = Entry + 1.5–1.8 × ATR_1H
TP2 = Entry + 3.0–3.5 × ATR_1H
```

Jangan berhenti pada ATR.

Perhatikan structural level:

```text
support
resistance
swing high
swing low
```

Target sebaiknya dapat mempertimbangkan keduanya.

Contoh:

```text
SL = structural invalidation constrained by ATR
TP1 = nearest structural resistance or valid ATR target
TP2 = next resistance or extended ATR target
```

---

# 6.4 Outcome Metrics

Minimal hitung:

```text
Win Rate
Loss Rate
Profit Factor
Expectancy
Average MFE
Average MAE
Median Time to TP1
Median Time to SL
Timeout Rate
Filled Rate
```

Jangan hanya menggunakan Win Rate.

---

# 6.5 A/B Test v1 vs v2

Perbandingan harus pada:

```text
same timestamp
same universe
same forward window
same data availability
```

Bandingkan:

```text
v1
vs
v2
```

dengan outcome:

```text
TP1 hit
SL hit
MFE
MAE
timeout
```

Pertanyaan yang harus dijawab:

```text
Apakah v2 mengurangi false BUY pada kondisi sideways?
Apakah v2 meningkatkan quality of triggered setups?
Apakah v2 mengurangi chasing setelah pump?
Apakah v2 memiliki expectancy yang lebih baik out-of-sample?
```

---

# 6.6 Bobot

Jangan menganggap:

```text
25 / 30 / 20 / 15 / 10
```

sebagai jawaban final.

Boleh diuji beberapa konfigurasi:

```text
Experiment A
25 / 30 / 20 / 15 / 10

Experiment B
30 / 30 / 15 / 15 / 10

Experiment C
25 / 35 / 15 / 15 / 10
```

Tetapi hanya gunakan hasil out-of-sample untuk memilih konfigurasi akhir.

---

# 6.7 Grid Search

Grid search diperbolehkan, tetapi:

```text
parameter search ≠ proof of edge
```

Grid search harus:

```text
fit on train
evaluate on validation
freeze parameters
evaluate on test
```

Hindari memilih model berdasarkan test performance.

---

# 6.8 MFE / MAE Analysis

Gunakan MFE/MAE untuk menjawab:

```text
Apakah SL terlalu ketat?
Apakah TP terlalu jauh?
Apakah setup momentum biasanya bergerak cepat?
Apakah accumulation membutuhkan holding time lebih panjang?
```

Contoh:

```text
banyak signal:
MAE -0.7%
MFE +5.8%
SL -3.5%
```

berarti stop terlalu longgar untuk setup tersebut.

Sebaliknya:

```text
MAE -2.8%
MFE +6.5%
SL -1.0%
```

dapat menunjukkan stop terlalu ketat.

Ini harus dibuktikan dari dataset, bukan intuisi.

---

# 6.9 Calibration Lab Output

Simpan:

```text
calibrated_config.json
```

Minimal:

```json
{
  "version": "v2",
  "created_at": "...",
  "dataset_range": {
    "train": "...",
    "validation": "...",
    "test": "..."
  },
  "weights": {
    "structure": 25,
    "momentum": 30,
    "flow": 20,
    "derivatives": 15,
    "news": 10
  },
  "thresholds": {},
  "atr_parameters": {},
  "time_stop_hours": 6,
  "metrics": {}
}
```

Jangan menyimpan hanya angka terbaik. Simpan juga metadata eksperimen.

---

# Final Acceptance Criteria

KripikTo v2 dianggap selesai apabila seluruh kondisi berikut terpenuhi:

```text
[ ] Fase 1 data integrity selesai
[ ] Fase 2 MTF pipeline selesai
[ ] Fase 3 outcome tracker selesai
[ ] Fase 4 feature engine selesai
[ ] Fase 5 state classification selesai
[ ] Fase 6 empirical validation selesai

[ ] closed candle only
[ ] no lookahead
[ ] 4H structure
[ ] 1H momentum
[ ] 15M trigger
[ ] BTC benchmark
[ ] RS_4H
[ ] RS_1H
[ ] delta OI
[ ] delta Funding
[ ] MFE
[ ] MAE
[ ] filled / unfilled
[ ] timeout
[ ] v1 vs v2 snapshot
[ ] setup taxonomy
[ ] dynamic ATR exits
[ ] structural exits
[ ] train/validation/test atau walk-forward
[ ] out-of-sample evaluation
```

---

# Aturan Keras untuk AI Agent

## 1. Jangan mengubah banyak hal sekaligus

Kerjakan phase-by-phase.

Setelah setiap phase:

```text
implement
→ compile
→ unit test
→ smoke test
→ database migration test
→ document changes
```

---

## 2. Jangan menghapus fitur v1 terlalu cepat

Pertahankan:

```text
v1_score
v1_recommendation
```

selama masa transisi.

Tujuannya untuk A/B testing.

---

## 3. Jangan hardcode threshold tanpa eksperimen

Hindari keputusan seperti:

```python
if volume_ratio >= 2:
```

sebagai nilai final hanya karena masuk akal secara intuitif.

Nilai awal boleh digunakan sebagai baseline, tetapi harus dapat dikalibrasi.

---

## 4. Jangan menggunakan forming candle

Candle dengan:

```text
is_closed = 0
```

tidak boleh digunakan dalam historical scanner.

---

## 5. Jangan menyebut score sebagai probabilitas

Contoh:

```text
score = 80
```

tidak berarti:

```text
80% chance price goes up
```

Score hanyalah composite signal strength sampai ada kalibrasi probabilitas yang valid.

---

## 6. Jangan menyamakan setup dengan outcome

```text
MOMENTUM_RUNNER
```

berarti kondisi feature memenuhi definisi setup.

Itu tidak berarti harga pasti naik.

```text
TRIGGERED
```

berarti tactical condition terpenuhi.

Itu tidak berarti trade pasti profit.

---

## 7. Jangan membuat scoring ganda

Final architecture harus jelas.

Gunakan salah satu:

```text
Structure 25
Momentum 30
Flow 20
Derivatives 15
AI News 10
= 100
```

Jika Risk Guard diperlukan, gunakan sebagai explicit safety override.

Jangan membuat:

```text
V2 score
→ 70% technical + 30% news
→ lalu news diberi bobot lagi
```

---

# Target Arsitektur Akhir

```text
                  TOP LIQUID USDT UNIVERSE
                            │
                            ▼
                   ┌────────────────┐
                   │   4H STRUCTURE  │
                   └───────┬────────┘
                           │
                     RS vs BTC 4H
                           │
                           ▼
                   ┌────────────────┐
                   │  1H MOMENTUM   │
                   └───────┬────────┘
                           │
                 ROC / ATR / Volume
                 OI Δ / Funding Δ
                 RS vs BTC 1H
                           │
                           ▼
                   ┌────────────────┐
                   │ 15M TRIGGER    │
                   └───────┬────────┘
                           │
                  Breakout / Retest
                           │
                           ▼
                 ┌───────────────────┐
                 │ STATE CLASSIFIER  │
                 └─────────┬─────────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
        ▼                  ▼                  ▼
 ACCUMULATION_COIL  MOMENTUM_RUNNER  VOLATILITY_SQUEEZE
        │                  │                  │
        ▼                  ▼                  ▼
       WAIT              READY/             WAIT
                         TRIGGERED
                           │
                           ▼
                     TRADE SNAPSHOT
                           │
                           ▼
                    OUTCOME TRACKER
                           │
                ┌──────────┴──────────┐
                │                     │
              MFE/MAE             TP/SL/Timeout
                │                     │
                └──────────┬──────────┘
                           ▼
                    CALIBRATION LAB
                           │
                   Train / Validation
                           │
                       Test / Walk
                         Forward
                           │
                           ▼
                V2 CONFIG + A/B RESULT
```

---

# Definition of Done

KripikTo v2 **jangan dianggap selesai hanya karena kode berjalan**.

Definition of Done:

> Sistem dapat menghasilkan sinyal MTF yang bebas lookahead, menyimpan snapshot lengkap, mengevaluasi outcome secara deterministik, membandingkan v1 vs v2 pada dataset yang sama, dan menunjukkan performa out-of-sample dari parameter yang digunakan.

Run berhasil:

```text
≠
strategy terbukti profitable
```

Compile berhasil:

```text
≠
statistical edge terbukti
```

Score tinggi:

```text
≠
harga pasti naik
```

Semua klaim tentang performa harus berasal dari outcome data dan evaluasi out-of-sample.
