"""
backend/feature_engine.py
Engine Fitur Kuantitatif & Klasifikasi Status Pasar (KripikTo v2):
1. Menghitung indikator momentum kinetik 1H:
   - ROC 1H (Rate of Change)
   - Price Acceleration 1H (Turunan kedua / perubahan slope kecepatan)
   - Volatility Expansion (ATR 1H / SMA ATR 1H)
   - Volume Surge 1H
   - Relative Strength 1H vs BTC
2. Memisahkan taksonomi secara tegas:
   - ACCUMULATION_COIL (Paus akumulasi diam-diam, harga belum lari -> STATUS: WAIT)
   - MOMENTUM_RUNNER (Struktur kuat + kinetik akselerasi meledak -> STATUS: READY)
   - VOLATILITY_SQUEEZE (Pita volatilitas menyempit, persiapan ekspansi -> STATUS: WAIT)
   - NO_SETUP / BEARISH (Tren lemah)
3. Menghitung Target Profit & Stop Loss Adaptif berbasis ATR 1H & Support Struktural
   (menggantikan dogma statis +6% berdasarkan temuan empiris MFE +4.5% & MAE -2.93%).
"""

import math
from typing import Dict, List, Any, Tuple, Optional
import pandas as pd
import numpy as np


# Bobot Modular Skoring 5 Pilar KripikTo v2 (Dapat Dikalibrasi via Outcome Tracker)
DEFAULT_WEIGHTS = {
    "structure": 25,
    "momentum": 30,
    "flow": 20,
    "derivatives": 15,
    "news": 10
}


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Menghitung Relative Strength Index (RSI 14)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Menghitung Average True Range (ATR 14) untuk mengukur volatilitas nyata koin."""
    prev_close = df.groupby("symbol")["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.groupby(df["symbol"]).transform(lambda x: x.rolling(period, min_periods=5).mean())
    return atr.fillna(df["close"] * 0.035)


def extract_1h_momentum_features(
    df_1h: pd.DataFrame,
    btc_ret_1h: float = 0.0
) -> Dict[str, Dict[str, Any]]:
    """
    Mengekstraksi fitur momentum kinetik dari data lilin 1H tertutup:
    - ROC 1H & Price Acceleration (Akselerasi harga)
    - Volatility Expansion Ratio (ATR 1H sekarang vs SMA 20 ATR 1H)
    - Volume Surge 1H
    - Relative Strength 1H vs BTC
    """
    if df_1h.empty:
        return {}

    # Hitung indikator 1H
    df = df_1h.copy()
    df["prev_close"] = df.groupby("symbol")["close"].shift(1)
    df["prev_close_2"] = df.groupby("symbol")["close"].shift(2)
    df["roc_1h"] = ((df["close"] - df["prev_close"]) / df["prev_close"].replace(0, np.nan)) * 100.0
    df["roc_prev_1h"] = ((df["prev_close"] - df["prev_close_2"]) / df["prev_close_2"].replace(0, np.nan)) * 100.0
    df["acceleration_1h"] = df["roc_1h"] - df["roc_prev_1h"]

    # ATR 1H dan Moving Average ATR 1H (untuk mendeteksi Volatility Expansion)
    df["atr_1h"] = calculate_atr(df, period=14)
    df["atr_ma20_1h"] = df.groupby("symbol")["atr_1h"].transform(lambda x: x.rolling(20, min_periods=5).mean())
    df["atr_expansion_ratio"] = df["atr_1h"] / df["atr_ma20_1h"].replace(0, np.nan)

    # Volume 1H dan MA Volume 1H
    df["vol_ma20_1h"] = df.groupby("symbol")["volume"].transform(lambda x: x.rolling(20, min_periods=5).mean())
    df["vol_ratio_1h"] = df["volume"] / df["vol_ma20_1h"].replace(0, np.nan)

    # Ambil baris tertutup terakhir per simbol
    latest_1h = df.groupby("symbol").last().reset_index()

    feature_map = {}
    for _, row in latest_1h.iterrows():
        sym = row["symbol"]
        roc = float(row["roc_1h"]) if pd.notnull(row["roc_1h"]) else 0.0
        accel = float(row["acceleration_1h"]) if pd.notnull(row["acceleration_1h"]) else 0.0
        atr_exp = float(row["atr_expansion_ratio"]) if pd.notnull(row["atr_expansion_ratio"]) else 1.0
        vol_r = float(row["vol_ratio_1h"]) if pd.notnull(row["vol_ratio_1h"]) else 1.0
        atr_v = float(row["atr_1h"]) if pd.notnull(row["atr_1h"]) else (float(row["close"]) * 0.02)
        close_p = float(row["close"])

        # RS 1H vs BTC
        rs_1h = round(roc - btc_ret_1h, 2)

        feature_map[sym] = {
            "close_1h": close_p,
            "roc_1h": round(roc, 2),
            "acceleration_1h": round(accel, 2),
            "atr_1h": atr_v,
            "atr_expansion_ratio": round(atr_exp, 2),
            "vol_ratio_1h": round(vol_r, 2),
            "rs_1h": rs_1h
        }

    return feature_map


def classify_setup_and_status(
    structure_score: int,
    momentum_score: int,
    flow_score: int,
    atr_expansion: float,
    roc_1h: float,
    rs_1h: float,
    is_overextended: bool = False
) -> Tuple[str, str, List[str]]:
    """
    Memisahkan Taksonomi Sinyal secara Jujur:
    - ACCUMULATION_COIL: Paus borong, struktur bagus, tapi momentum tidur -> STATUS: WAIT
    - MOMENTUM_RUNNER: Struktur bagus, akselerasi kinetik meledak -> STATUS: READY
    - VOLATILITY_SQUEEZE: Volatilitas tertekan rapat, tunggu penembusan -> STATUS: WAIT
    - NO_SETUP: Kondisi lemah
    """
    tags = []

    # 1. Deteksi SETUP TYPE
    if structure_score >= 15 and flow_score >= 12 and momentum_score < 14:
        # Kasus PARTI: Akumulasi kuat tetapi kinetik belum bergerak
        setup_type = "ACCUMULATION_COIL"
        tags.append("PAUS_NYICIL_DIAM (Harga Belum Gerak)")
    elif structure_score >= 16 and momentum_score >= 16:
        # Momentum Runner: Struktur dan akselerasi sejalan
        setup_type = "MOMENTUM_RUNNER"
        tags.append("MOMENTUM_EXPANDING (Akselerasi Positif)")
    elif atr_expansion < 0.85 and structure_score >= 14:
        setup_type = "VOLATILITY_SQUEEZE"
        tags.append("VOLATILITY_SQUEEZE (Pita Menyempit)")
    elif structure_score >= 15:
        setup_type = "STRUCTURE_BULLISH"
    else:
        setup_type = "NO_SETUP"

    # 2. Deteksi ENTRY STATUS
    if setup_type == "MOMENTUM_RUNNER":
        if is_overextended:
            entry_status = "EXTENDED"
            tags.append("⚠️ JANGAN_KEJAR (Tunggu Retest)")
        else:
            entry_status = "READY"
            tags.append("🚀 READY (Menunggu 15M Trigger)")
    elif setup_type == "ACCUMULATION_COIL":
        entry_status = "WAIT"
        tags.append("⏳ WAIT (Masuk Watchlist, Jangan Beli Sekarang)")
    elif setup_type == "VOLATILITY_SQUEEZE":
        entry_status = "WAIT"
        tags.append("⏳ WAIT (Tunggu Breakout Volatilitas)")
    else:
        entry_status = "WAIT"

    return setup_type, entry_status, tags


def calculate_dynamic_tp_sl(
    entry_price: float,
    support_level: float,
    atr_val: float,
    dec: int = 4
) -> Dict[str, Any]:
    """
    Menghitung TP & SL Dinamis Berbasis ATR & Support Struktural:
    - TP1: Berdasarkan 1.5x ATR (biasanya +3.5% s.d +4.8%, sesuai data empiris MFE rata-rata +4.5%)
    - TP2: Berdasarkan 3.0x ATR (runner target)
    - SL : Berdasarkan Support struktural & 1.1x ATR (terjaga di -3.8% s.d -4.5%, aman dari MAE -2.93%)
    """
    if entry_price <= 0:
        entry_price = 1e-8

    # 1. Stop Loss: Gabungkan Support Struktural dan Toleransi ATR
    # Sesuai data Outcome Tracker: MAE rata-rata -2.93%, jadi SL ideal -3.8% s.d -4.5%
    structural_sl = min(support_level * 0.988, entry_price - (1.1 * atr_val))
    sl_price = round(min(entry_price * 0.962, max(entry_price * 0.945, structural_sl)), dec)
    sl_pct = round(((sl_price - entry_price) / entry_price) * 100.0, 2)

    # 2. Take Profit 1: Minimal 3.5% atau 1.5x ATR (bukan +6% kaku)
    atr_tp1_dist = max(entry_price * 0.035, 1.5 * atr_val)
    tp1_price = round(entry_price + atr_tp1_dist, dec)
    tp1_pct = round(((tp1_price - entry_price) / entry_price) * 100.0, 2)

    # 3. Take Profit 2 (Runner): Minimal 7.0% atau 3.0x ATR
    atr_tp2_dist = max(entry_price * 0.070, 3.0 * atr_val)
    tp2_price = round(entry_price + atr_tp2_dist, dec)
    tp2_pct = round(((tp2_price - entry_price) / entry_price) * 100.0, 2)

    rr_ratio = f"1 : {round(abs(tp1_pct / sl_pct), 1)}" if abs(sl_pct) > 0 else "1 : 1.5"

    return {
        "stop_loss": sl_price,
        "stop_loss_pct": sl_pct,
        "tp1": tp1_price,
        "tp1_pct": tp1_pct,
        "tp2": tp2_price,
        "tp2_pct": tp2_pct,
        "risk_reward": rr_ratio
    }
