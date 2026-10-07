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
import json
from pathlib import Path
from typing import Dict, List, Any, Tuple, Optional
import pandas as pd
import numpy as np

try:
    from backend.data_integrity import aligned_relative_strength
except ImportError:
    from data_integrity import aligned_relative_strength


CONFIG_PATH = Path(__file__).resolve().parent / "data" / "calibrated_config.json"


def load_calibrated_config() -> Dict[str, Any]:
    """Membaca konfigurasi empiris dari calibrated_config.json jika tersedia (Fase 6)."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
                return config if config.get("version") == SCORING_VERSION else {}
        except Exception:
            pass
    return {}


BASE_WEIGHTS = {"structure": 25, "momentum": 30, "flow": 20, "derivatives": 15, "news": 10}
SCORING_VERSION = "v2.1-normalized"


def validate_weights(weights):
    if set(weights) != set(BASE_WEIGHTS):
        raise ValueError("weights must contain the five pillars.")
    result = {name: float(value) for name, value in weights.items()}
    if any(not math.isfinite(v) or v <= 0 for v in result.values()) or not math.isclose(sum(result.values()), 100):
        raise ValueError("Positive finite weights must total 100.")
    return result


def compose_quant_score(components, weights=None, penalty=0.0):
    """Raw subscores retain baseline units; weighting never changes setup thresholds."""
    weights = validate_weights(BASE_WEIGHTS if weights is None else weights)
    score = sum(max(0.0, min(BASE_WEIGHTS[k], float(components.get(k, 0)))) / BASE_WEIGHTS[k] * weights[k]
                for k in BASE_WEIGHTS if k != "news")
    return round(max(0.0, min(100 - weights["news"], score - float(penalty))), 6)


# Bobot Modular Skoring 5 Pilar KripikTo v2 (Dapat Dikalibrasi via Outcome Tracker / Calibration Lab)
_CALIBRATED_CFG = load_calibrated_config()
DEFAULT_WEIGHTS = _CALIBRATED_CFG.get("weights", {
    "structure": 25,
    "momentum": 30,
    "flow": 20,
    "derivatives": 15,
    "news": 10
})


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI dengan rata-rata rolling; warm-up dan harga datar bernilai netral 50."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    rsi = rsi.mask((avg_loss == 0) & (avg_gain == 0), 50.0)
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
    btc_returns: Optional[Dict[tuple, float]] = None,
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
    df["prev_open_time"] = df.groupby("symbol")["open_time"].shift(1)
    df["prev_close_time"] = df.groupby("symbol")["close_time"].shift(1)
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
    latest_1h = df.groupby("symbol", sort=False).tail(1).reset_index(drop=True)

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
        rs_1h, btc_matched, rs_status = aligned_relative_strength(
            row, btc_returns or {}, float(row["roc_1h"]),
        )

        feature_map[sym] = {
            "close_1h": close_p,
            "roc_1h": round(roc, 2),
            "acceleration_1h": round(accel, 2),
            "atr_1h": atr_v,
            "atr_expansion_ratio": round(atr_exp, 2),
            "vol_ratio_1h": round(vol_r, 2),
            "rs_1h": rs_1h,
            "rs_1h_status": rs_status,
            "btc_return_1h": btc_matched,
            "open_time_1h": int(row["open_time"]),
            "close_time_1h": int(row["close_time"]),
        }

    return feature_map


def classify_setup_and_status(
    structure_score: int,
    momentum_score: int,
    flow_score: int,
    atr_expansion: float,
    roc_1h: float,
    rs_1h: float,
    vol_ratio_4h: float = 1.0,
    is_overextended: bool = False
) -> Tuple[str, str, List[str]]:
    """
    Memisahkan Taksonomi Sinyal sesuai Spesifikasi Desain KripikTo v2 (Fase 5):
    - ACCUMULATION_COIL: Structure >= 18, Flow >= 14, Momentum < 12 (STATUS: WAIT)
    - MOMENTUM_RUNNER: Structure >= 18, Momentum >= 22, Flow >= 14 (STATUS: READY / EXTENDED)
    - VOLATILITY_SQUEEZE: ATR expansion < 0.85 DAN Volume Kering <= 0.80x (STATUS: WAIT)
    - STRUCTURE_BULLISH: Structure >= 15 (Tren sehat tetapi belum runner)
    - NO_SETUP: Kondisi lemah
    """
    tags = []

    # 1. Deteksi SETUP TYPE Berdasarkan Definisi Spesifikasi
    if structure_score >= 18 and flow_score >= 14 and momentum_score < 12:
        # Kasus PARTI: Akumulasi paus kuat tetapi momentum kinetik belum berkembang
        setup_type = "ACCUMULATION_COIL"
        tags.append("PAUS_NYICIL_DIAM (Harga Belum Gerak)")
    elif structure_score >= 18 and momentum_score >= 22 and flow_score >= 14:
        # Momentum Runner: Struktur, aliran paus, dan kinetik momentum akselerasi sejalan
        setup_type = "MOMENTUM_RUNNER"
        tags.append("MOMENTUM_RUNNER (Akselerasi Kinetik Positif)")
    elif structure_score >= 18 and flow_score >= 14 and 12 <= momentum_score < 22:
        # Explicit transition state between accumulation and a full momentum runner.
        setup_type = "MOMENTUM_FORMING"
        tags.append("MOMENTUM_FORMING (Energi Mulai Terbentuk)")
    elif atr_expansion < 0.85 and vol_ratio_4h <= 0.80 and structure_score >= 14:
        # Volatility Squeeze Sejati: Pita volatilitas menyempit DAN volume perdagangan kering
        setup_type = "VOLATILITY_SQUEEZE"
        tags.append("VOLATILITY_SQUEEZE (Pita Menyempit + Volume Kering)")
    elif structure_score >= 15:
        setup_type = "STRUCTURE_BULLISH"
    else:
        setup_type = "NO_SETUP"

    # 2. Deteksi ENTRY STATUS (Prinsip: WAIT != BUY, READY != BUY, EXTENDED != BUY)
    if is_overextended:
        entry_status = "EXTENDED"
        tags.append("⚠️ JANGAN_KEJAR (Tunggu Retest)")
    elif setup_type == "MOMENTUM_RUNNER":
        entry_status = "READY"
        tags.append("🚀 READY (Menunggu 15M Trigger)")
    elif setup_type == "ACCUMULATION_COIL":
        entry_status = "WAIT"
        tags.append("⏳ WAIT (Masuk Watchlist, Jangan Beli Sekarang)")
    elif setup_type == "MOMENTUM_FORMING":
        entry_status = "READY"
        tags.append("👀 READY (Momentum mulai terbentuk, tunggu 15M)")
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
    dec: int = 4,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Menghitung TP & SL Dinamis Berbasis ATR & Support Struktural:
    - TP1: Max lantai target konfigurasi dan multiplier ATR (default 4.5%, 1.6x).
    - TP2: Berdasarkan 3.0x ATR (runner target)
    - SL : Support/ATR dibatasi risiko maksimum stop_loss_pct dari konfigurasi.
    """
    if entry_price <= 0:
        entry_price = 1e-8

    cfg = load_calibrated_config() if config is None else config
    target_tp1_pct = float(cfg.get("target_profit_1_pct", 4.5))
    target_sl_pct = abs(float(cfg.get("stop_loss_pct", -4.8)))
    tp_mult = float(cfg.get("tp_atr_multiplier", 1.6))
    sl_mult = float(cfg.get("sl_atr_multiplier", 1.25))

    if (not all(math.isfinite(v) for v in (entry_price, support_level, atr_val, target_tp1_pct, tp_mult, sl_mult))
            or support_level <= 0 or atr_val <= 0 or target_tp1_pct <= 0 or tp_mult <= 0 or sl_mult <= 0):
        raise ValueError("Invalid ATR trading-plan parameters.")

    if not math.isfinite(target_sl_pct) or not 0 < target_sl_pct < 100:
        raise ValueError("stop_loss_pct harus memiliki magnitudo antara 0 dan 100.")

    # stop_loss_pct is a maximum loss distance, not a mandatory exact stop.
    structural_sl = min(support_level * 0.990, entry_price - (sl_mult * atr_val))
    max_sl_price = entry_price * (1.0 - (target_sl_pct / 100.0))
    # Round the risk floor upward so price precision cannot exceed the risk cap.
    price_scale = 10 ** dec
    risk_floor = math.ceil(max_sl_price * price_scale) / price_scale
    sl_price = max(round(structural_sl, dec), risk_floor)
    if not 0 < sl_price < entry_price:
        raise ValueError("Presisi harga atau parameter SL tidak menghasilkan stop di bawah entry.")
    sl_pct = round(((sl_price - entry_price) / entry_price) * 100.0, 2)

    # 2. Take Profit 1: Minimal target kalibrasi atau tp_mult * ATR
    atr_tp1_dist = max(entry_price * (target_tp1_pct / 100.0), tp_mult * atr_val)
    tp1_price = round(entry_price + atr_tp1_dist, dec)
    tp1_pct = round(((tp1_price - entry_price) / entry_price) * 100.0, 2)

    # 3. Take Profit 2 (Runner): Minimal 7.5% atau 3.0x ATR
    atr_tp2_dist = max(entry_price * 0.075, 3.0 * atr_val, atr_tp1_dist)
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


def evaluate_15m_trigger(
    df_15m: pd.DataFrame
) -> Dict[str, Dict[str, Any]]:
    """
    Mengevaluasi Tactical Entry Trigger pada lilin 15M tertutup (KripikTo v2 Fase 5):
    Mendeteksi titik eksekusi mikro yang presisi:
    1. BREAKOUT_TRIGGER: Close > 20-bar 15M High + Volume 15M Spike >= 1.25x + RSI < 78.
    2. RETEST_TRIGGER: Harga mengetes MA20 15M (jarak <= 0.8%) dengan pantulan bullish / lower-wick pinbar >= 35%.
    3. OVERBOUGHT_WARNING: RSI 15M >= 78 atau lonjakan lilin tunggal >= 4% -> STATUS: EXTENDED (Tunggu pullback).
    4. BREAKDOWN_WARNING: Close < MA20 15M * 0.985 -> STATUS: FAILED (Gagal bertahan di mikro support).
    5. CONSOLIDATING: Bergerak di atas MA20 15M -> STATUS: WAIT (Menunggu pemicu breakout/retest).
    """
    if df_15m.empty:
        return {}

    df = df_15m.copy()
    df["ma20_15m"] = df.groupby("symbol")["close"].transform(lambda x: x.rolling(20, min_periods=5).mean())
    df["vol_ma20_15m"] = df.groupby("symbol")["volume"].transform(lambda x: x.rolling(20, min_periods=5).mean())
    df["high20_15m"] = df.groupby("symbol")["high"].transform(lambda x: x.shift(1).rolling(20, min_periods=5).max())
    df["low20_15m"] = df.groupby("symbol")["low"].transform(lambda x: x.shift(1).rolling(20, min_periods=5).min())
    df["rsi14_15m"] = df.groupby("symbol")["close"].transform(lambda x: calculate_rsi(x, period=14))
    df["vol_ratio_15m"] = df["volume"] / df["vol_ma20_15m"].replace(0, np.nan)

    latest_15m = df.groupby("symbol").last().reset_index()
    trigger_map = {}

    for _, row in latest_15m.iterrows():
        sym = row["symbol"]
        close = float(row["close"])
        open_p = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        vol_r = float(row["vol_ratio_15m"]) if pd.notnull(row["vol_ratio_15m"]) else 1.0
        ma20 = float(row["ma20_15m"]) if pd.notnull(row["ma20_15m"]) else close
        high20 = float(row["high20_15m"]) if pd.notnull(row["high20_15m"]) else high
        rsi_val = float(row["rsi14_15m"]) if pd.notnull(row["rsi14_15m"]) else 50.0

        candle_range = max(high - low, 1e-9)
        lower_wick = (open_p - low) if close >= open_p else (close - low)
        lower_wick_ratio = lower_wick / candle_range

        # Deteksi kondisi pemicu taktis 15M
        is_breakout = (close > high20 and high20 > 0 and vol_r >= 1.25 and rsi_val < 78.0)
        dist_to_ma20 = abs(close - ma20) / ma20 if ma20 > 0 else 0.0
        is_retest_bounce = (dist_to_ma20 <= 0.008 and (close >= open_p or lower_wick_ratio >= 0.35) and 45.0 <= rsi_val <= 68.0)
        is_overbought = (rsi_val >= 78.0 or ((close - open_p) / open_p * 100.0) >= 4.0)
        is_breakdown = (close < ma20 * 0.985)

        tags = []
        if is_overbought:
            status = "EXTENDED"
            reason = f"⚠️ 15M_OVERBOUGHT (RSI: {rsi_val:.0f}, Tunggu Pullback)"
            tags.append("15M_OVERBOUGHT")
        elif is_breakdown:
            status = "FAILED"
            reason = f"❌ 15M_BREAKDOWN (Jatuh di Bawah MA20 15M)"
            tags.append("15M_BREAKDOWN")
        elif is_breakout:
            status = "TRIGGERED"
            reason = f"🎯 15M_BREAKOUT_VOL (Vol: {vol_r:.1f}x, RSI: {rsi_val:.0f})"
            tags.append("15M_BREAKOUT_CONFIRMED")
        elif is_retest_bounce:
            status = "TRIGGERED"
            reason = f"🎯 15M_RETEST_BOUNCE (Dekat MA20, Wick: {int(lower_wick_ratio*100)}%)"
            tags.append("15M_RETEST_BOUNCE_CONFIRMED")
        else:
            status = "WAIT"
            reason = f"⏳ 15M_KONSOLIDASI (RSI: {rsi_val:.0f}, Menunggu Pemicu)"
            tags.append("15M_CONSOLIDATING")

        # Mikro support untuk memperketat buy area
        micro_support = max(ma20, low)
        dec = 8 if close < 0.01 else (6 if close < 1.0 else 4)
        refined_buy_low = round(max(micro_support * 0.995, close * 0.985), dec)
        refined_buy_high = round(close * 0.998, dec)
        if refined_buy_low >= refined_buy_high:
            refined_buy_low = round(close * 0.988, dec)

        trigger_map[sym] = {
            "trigger_status": status,
            "trigger_reason": reason,
            "vol_ratio_15m": round(vol_r, 2),
            "rsi_15m": round(rsi_val, 1),
            "micro_support": round(micro_support, dec),
            "refined_buy_low": refined_buy_low,
            "refined_buy_high": refined_buy_high,
            "tags": tags
        }

    return trigger_map
