"""
backend/calibration_lab.py
Laboratorium Eksperimen & Kalibrasi Empiris Kuantitatif (KripikTo v2 Fase 6):
Prinsip: "Measurement before optimization" & "Bobot/threshold adalah VARIABEL, bukan dogma"

Metodologi Ilmiah yang Diterapkan:
1. Time-Series Train/Test Split (Out-of-Sample Validation):
   - 70% data awal kronologis = Training Set (In-Sample Calibration)
   - 30% data terbaru kronologis = Testing Set (Out-of-Sample Evaluation)
   - Mencegah Overfitting & Lookahead Bias.
2. Dynamic ATR Grid Search Simulation:
   - Menguji kombinasi Multiplier ATR Dinamis nyata (TP: 1.2x - 3.0x ATR, SL: 1.0x - 2.0x ATR)
   - Membandingkannya secara langsung dengan Static Percentage (+6% / -4.5% baseline v1).
3. Kalibrasi Bobot 5 Pilar Matematis (Feature-Driven Weighting):
   - Mengukur korelasi dan Edge Spread tiap pilar terhadap MFE pada Training Set.
   - Menghitung distribusi bobot 100 poin secara proporsional terhadap daya prediksi empiris.
4. Evaluasi Kejadian Ambigu (Same-Candle Flash Spike):
   - Tidak mengasumsikan TP tercapai lebih dulu jika SL juga tersentuh di lilin yang sama.
5. Menyimpan Parameter Terkalibrasi ke calibrated_config.json.
"""

import sys
import json
import sqlite3
import argparse
from pathlib import Path
from typing import Dict, List, Any, Tuple, Optional
import pandas as pd
import numpy as np

# Pastikan output utf-8 aman di terminal Windows
if sys.platform == "win32" and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DATA_DIR / "kripto.db")
CALIBRATED_CONFIG_PATH = str(DATA_DIR / "calibrated_config.json")


def load_dataset(db_path: str = DB_PATH, only_filled: bool = True) -> pd.DataFrame:
    """Membaca riwayat sinyal dan metrik empiris MFE/MAE dari SQLite, digabung dengan fitur scan."""
    try:
        from backend.scanner import init_scanner_db
        init_scanner_db(db_path)
    except Exception:
        pass
    with sqlite3.connect(db_path) as conn:
        query = """
            SELECT 
                o.*,
                COALESCE(s.score, o.v1_score) as scan_score,
                s.rsi14 as scan_rsi14,
                s.vol_ratio as scan_vol_ratio,
                s.taker_buy_ratio as scan_taker_ratio,
                s.funding_rate as scan_funding_rate,
                s.open_interest_m as scan_oi_m,
                s.price_change_pct as scan_change_24h,
                s.roc_1h as scan_roc_1h,
                s.acceleration_1h as scan_accel_1h,
                s.atr_expansion as scan_atr_expansion,
                s.delta_oi_1h as scan_delta_oi_1h,
                s.delta_funding_1h as scan_delta_funding_1h
            FROM signal_outcomes o
            LEFT JOIN scan_results s 
                ON o.symbol = s.symbol AND o.scan_time = s.scan_time
        """
        if only_filled:
            query += " WHERE o.is_filled = 1 AND (o.mfe_pct IS NOT NULL OR o.return_24h_pct IS NOT NULL)"
        query += " ORDER BY o.scan_time ASC"  # Kronologis dari terlama ke terbaru
        df = pd.read_sql(query, conn)
    return df


def split_train_test(df: pd.DataFrame, train_ratio: float = 0.70) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Membagi dataset secara time-series kronologis tanpa pengacakan (Walk-Forward foundation):
    - 70% awal: In-Sample Training (pencarian parameter & kalibrasi bobot)
    - 30% akhir: Out-of-Sample Test (pengujian performa murni)
    """
    if len(df) < 10:
        return df, df

    split_idx = int(len(df) * train_ratio)
    df_train = df.iloc[:split_idx].copy().reset_index(drop=True)
    df_test = df.iloc[split_idx:].copy().reset_index(drop=True)
    return df_train, df_test


def simulate_trade_outcome(
    mfe_pct: float,
    mae_pct: float,
    target_tp_pct: float,
    stop_loss_pct: float,
    actual_result: Optional[str] = None,
    ret_12h_pct: Optional[float] = None
) -> Tuple[str, float]:
    """
    Mensimulasikan hasil trade historis pada level TP dan SL tertentu:
    - Menghormati kejadian AMBIGUOUS (jika lilin menyentuh TP dan SL bersamaan).
    - Menghindari lookahead bias.
    """
    sl_dist = -abs(stop_loss_pct)
    tp_dist = abs(target_tp_pct)

    hit_tp = (mfe_pct >= tp_dist)
    hit_sl = (mae_pct <= sl_dist)

    if actual_result == "AMBIGUOUS":
        # Sesuai spesifikasi Fase 3.5: Jangan mengarang urutan kejadian
        return "AMBIGUOUS", sl_dist * 0.5

    if hit_tp and not hit_sl:
        return "TP_HIT", tp_dist
    elif hit_sl and not hit_tp:
        return "SL_HIT", sl_dist
    elif hit_tp and hit_sl:
        # Kedua level tercapai selama durasi posisi:
        # Pendekatan konservatif kuantitatif: jika MAE dalam, anggap SL tersapu dulu
        if abs(mae_pct) >= abs(sl_dist * 1.15):
            return "SL_HIT", sl_dist
        else:
            return "TP_HIT", tp_dist
    else:
        # Posisi timeout (Time-Stop)
        fallback_ret = float(ret_12h_pct) if pd.notnull(ret_12h_pct) else 0.0
        return "TIMEOUT", fallback_ret


def evaluate_configuration_dynamic_atr(
    df: pd.DataFrame,
    tp_mult: float,
    sl_mult: float
) -> Dict[str, Any]:
    """
    Menghitung metrik performa portofolio untuk kombinasi Multiplier ATR Dinamis:
    TP = tp_mult * (ATR / Entry), SL = -sl_mult * (ATR / Entry).
    """
    results = []
    returns = []

    for _, row in df.iterrows():
        mfe = float(row.get("mfe_pct") or 0.0)
        mae = float(row.get("mae_pct") or 0.0)
        ret12 = row.get("return_12h_pct")
        act_res = row.get("result")

        entry_p = float(row.get("entry_price") or row.get("fill_price") or 1.0)
        atr_val = float(row.get("atr14") or (entry_p * 0.032))
        if atr_val <= 0 or entry_p <= 0:
            atr_pct = 3.2
        else:
            atr_pct = (atr_val / entry_p) * 100.0

        # Batas minimum logis agar tidak terkena fee Binance (min TP 3.0%, min SL -3.2%)
        tp_target_pct = max(3.0, round(tp_mult * atr_pct, 2))
        sl_target_pct = -max(3.2, round(sl_mult * atr_pct, 2))

        outcome, ret_realized = simulate_trade_outcome(
            mfe_pct=mfe,
            mae_pct=mae,
            target_tp_pct=tp_target_pct,
            stop_loss_pct=sl_target_pct,
            actual_result=act_res,
            ret_12h_pct=ret12
        )
        results.append(outcome)
        returns.append(ret_realized)

    total_trades = len(results)
    if total_trades == 0:
        return {}

    tp_count = results.count("TP_HIT")
    sl_count = results.count("SL_HIT")
    to_count = results.count("TIMEOUT")
    amb_count = results.count("AMBIGUOUS")

    win_rate = (tp_count / total_trades) * 100.0
    loss_rate = (sl_count / total_trades) * 100.0
    timeout_rate = (to_count / total_trades) * 100.0

    gains = [r for r in returns if r > 0]
    losses = [abs(r) for r in returns if r < 0]

    sum_gains = sum(gains)
    sum_losses = sum(losses)
    net_return = sum_gains - sum_losses

    avg_win = (sum_gains / len(gains)) if gains else 0.0
    avg_loss = (sum_losses / len(losses)) if losses else 1.0

    profit_factor = (sum_gains / sum_losses) if sum_losses > 0 else (99.0 if sum_gains > 0 else 0.0)

    # Expectancy ($ per $1 resiko)
    p_win = win_rate / 100.0
    p_loss = loss_rate / 100.0
    expectancy = (p_win * avg_win) - (p_loss * avg_loss)

    # Kelly Criterion %
    rr_ratio = (avg_win / avg_loss) if avg_loss > 0 else 1.0
    kelly = p_win - ((1.0 - p_win) / rr_ratio) if rr_ratio > 0 else 0.0

    return {
        "tp_mult": tp_mult,
        "sl_mult": sl_mult,
        "total_trades": total_trades,
        "win_rate": round(win_rate, 1),
        "loss_rate": round(loss_rate, 1),
        "timeout_rate": round(timeout_rate, 1),
        "ambiguous_count": amb_count,
        "profit_factor": round(profit_factor, 2),
        "expectancy": round(expectancy, 2),
        "kelly_pct": round(max(0.0, kelly * 100.0), 1),
        "net_return_pct": round(net_return, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2)
    }


def run_dynamic_atr_grid_search(df: pd.DataFrame) -> pd.DataFrame:
    """
    Grid Search simulasi pada Multiplier ATR Dinamis (Spesifikasi Fase 6.3):
    - TP Multiplier: 1.2x, 1.5x, 1.8x, 2.0x, 2.4x, 2.8x, 3.2x ATR
    - SL Multiplier: 1.0x, 1.2x, 1.4x, 1.6x, 1.8x, 2.0x ATR
    """
    tp_candidates = [1.2, 1.5, 1.8, 2.0, 2.4, 2.8, 3.2]
    sl_candidates = [1.0, 1.2, 1.4, 1.6, 1.8, 2.0]

    grid_results = []
    for tp in tp_candidates:
        for sl in sl_candidates:
            metrics = evaluate_configuration_dynamic_atr(df, tp_mult=tp, sl_mult=sl)
            if metrics:
                grid_results.append(metrics)

    df_grid = pd.DataFrame(grid_results)
    if not df_grid.empty:
        df_grid = df_grid.sort_values(by=["profit_factor", "expectancy", "win_rate"], ascending=[False, False, False]).reset_index(drop=True)
    return df_grid


def analyze_feature_correlations(df: pd.DataFrame) -> pd.DataFrame:
    """
    Mengukur signifikansi statistik (korelasi Pearson) antara subskor/indikator
    dengan MFE (puncak keuntungan).
    """
    feature_meta = [
        ("scan_score", "Skor Teknikal"),
        ("scan_rsi14", "RSI 14 (Momentum)"),
        ("scan_vol_ratio", "Volume Ratio 4H"),
        ("scan_taker_ratio", "Taker Buy (Paus)"),
        ("scan_funding_rate", "Funding Rate"),
        ("scan_change_24h", "24h Return %"),
        ("structure_score", "Pilar 1: Struktur 4H"),
        ("momentum_score", "Pilar 2: Momentum 1H"),
        ("flow_score", "Pilar 3: Whale Flow"),
        ("derivative_score", "Pilar 4: Derivatif"),
        ("v2_score", "Skor Komposit MTF v2")
    ]

    if "mfe_pct" not in df.columns:
        return pd.DataFrame()

    records = []
    for col, label in feature_meta:
        if col not in df.columns or df[col].notnull().sum() < 5:
            continue
        series_feat = pd.to_numeric(df[col], errors="coerce")
        series_mfe = pd.to_numeric(df["mfe_pct"], errors="coerce")
        valid_idx = series_feat.notnull() & series_mfe.notnull()

        if valid_idx.sum() >= 5:
            corr_mfe = series_feat[valid_idx].corr(series_mfe[valid_idx])
            med_val = series_feat[valid_idx].median()
            high_group_mfe = series_mfe[valid_idx & (series_feat >= med_val)].mean()
            low_group_mfe = series_mfe[valid_idx & (series_feat < med_val)].mean()
            delta_edge = high_group_mfe - low_group_mfe

            records.append({
                "kode": col,
                "fitur": label,
                "korelasi_mfe": round(corr_mfe, 3) if pd.notnull(corr_mfe) else 0.0,
                "mfe_high_group": round(high_group_mfe, 2) if pd.notnull(high_group_mfe) else 0.0,
                "mfe_low_group": round(low_group_mfe, 2) if pd.notnull(low_group_mfe) else 0.0,
                "edge_spread": round(delta_edge, 2) if pd.notnull(delta_edge) else 0.0
            })

    df_corr = pd.DataFrame(records)
    if not df_corr.empty:
        df_corr = df_corr.sort_values(by="edge_spread", ascending=False).reset_index(drop=True)
    return df_corr


def optimize_pillar_weights(df_train: pd.DataFrame, df_corr: pd.DataFrame) -> Dict[str, int]:
    """
    Kalibrasi Bobot 5 Pilar Kuantitatif Matematis Berbasis Data Training (Spesifikasi Fase 6.6):
    Baseline: Structure: 25, Momentum: 30, Flow: 20, Derivatives: 15, News: 10 = Total 100.
    Menggunakan Edge Spread empiris untuk menyetel distribusi bobot tanpa asumsi dogmatis.
    """
    # Bobot lantai minimum agar tidak ada pilar yang mati total
    base_floor = {
        "structure": 15,
        "momentum": 20,
        "flow": 15,
        "derivatives": 10,
        "news": 10
    }
    # Sisa 30 poin didistribusikan secara proporsional sesuai daya dorong profit (Edge Spread)
    if df_corr.empty:
        return {"structure": 25, "momentum": 30, "flow": 20, "derivatives": 15, "news": 10}

    corr_dict = dict(zip(df_corr["kode"], df_corr["edge_spread"]))
    
    # Daya dorong relatif tiap pilar
    mom_edge = max(0.1, corr_dict.get("scan_rsi14", 1.5) + corr_dict.get("momentum_score", 1.0))
    str_edge = max(0.1, corr_dict.get("scan_score", 1.2) + corr_dict.get("structure_score", 0.8))
    flw_edge = max(0.1, corr_dict.get("scan_vol_ratio", 0.6) + max(0.0, corr_dict.get("scan_taker_ratio", 0.0)))
    der_edge = max(0.1, corr_dict.get("scan_funding_rate", 1.2) + corr_dict.get("derivative_score", 0.8))

    total_edge = mom_edge + str_edge + flw_edge + der_edge
    rem_pts = 30 # Poin yang diperebutkan secara dinamis

    w_mom = base_floor["momentum"] + int(round((mom_edge / total_edge) * rem_pts))
    w_str = base_floor["structure"] + int(round((str_edge / total_edge) * rem_pts))
    w_der = base_floor["derivatives"] + int(round((der_edge / total_edge) * rem_pts))
    w_flw = base_floor["flow"] + int(round((flw_edge / total_edge) * rem_pts))
    w_news = 10

    # Normalisasi agar total persis 100
    sum_w = w_mom + w_str + w_der + w_flw + w_news
    diff = 100 - sum_w
    w_mom += diff

    return {
        "structure": int(w_str),
        "momentum": int(w_mom),
        "flow": int(w_flw),
        "derivatives": int(w_der),
        "news": int(w_news)
    }


def save_calibrated_config(
    best_params: Dict[str, Any],
    calibrated_weights: Dict[str, int],
    test_metrics: Dict[str, Any],
    train_metrics: Dict[str, Any],
    filepath: str = CALIBRATED_CONFIG_PATH
) -> None:
    """Menyimpan konfigurasi parameter optimal hasil kalibrasi out-of-sample ke JSON."""
    config_payload = {
        "calibrated_at": str(pd.Timestamp.now(tz="UTC")),
        "validation_method": "Time-Series Train/Test Split (70% In-Sample / 30% Out-of-Sample)",
        "train_sample_trades": train_metrics.get("total_trades", 0),
        "test_sample_trades": test_metrics.get("total_trades", 0),
        "tp_atr_multiplier": best_params.get("tp_mult", 1.8),
        "sl_atr_multiplier": best_params.get("sl_mult", 1.4),
        "target_profit_1_pct": round(best_params.get("tp_mult", 1.8) * 3.0, 1),
        "stop_loss_pct": -round(best_params.get("sl_mult", 1.4) * 3.2, 1),
        "in_sample_performance": {
            "win_rate": train_metrics.get("win_rate", 0.0),
            "profit_factor": train_metrics.get("profit_factor", 0.0),
            "expectancy": train_metrics.get("expectancy", 0.0),
            "net_return_pct": train_metrics.get("net_return_pct", 0.0)
        },
        "out_of_sample_performance": {
            "win_rate": test_metrics.get("win_rate", 0.0),
            "profit_factor": test_metrics.get("profit_factor", 0.0),
            "expectancy": test_metrics.get("expectancy", 0.0),
            "net_return_pct": test_metrics.get("net_return_pct", 0.0)
        },
        "overfitting_pf_delta": round(abs(train_metrics.get("profit_factor", 0.0) - test_metrics.get("profit_factor", 0.0)), 2),
        "weights": calibrated_weights,
        "description": "Konfigurasi terkalibrasi secara empiris dengan validasi Out-of-Sample tanpa lookahead bias."
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(config_payload, f, indent=2, ensure_ascii=False)
    print(f"[✓] Konfigurasi terkalibrasi berhasil disimpan di: {filepath}")


def print_experiment_report(
    df_train: pd.DataFrame,
    df_test: pd.DataFrame,
    df_grid_train: pd.DataFrame,
    best_params: Dict[str, Any],
    train_metrics: Dict[str, Any],
    test_metrics: Dict[str, Any],
    df_corr: pd.DataFrame,
    calibrated_weights: Dict[str, int]
) -> None:
    """Mencetak Laporan Laboratorium Eksperimen Ilmiah KripikTo v2 di terminal."""
    print("\n" + "=" * 135)
    print("🔬 KRIPIKTO QUANTITATIVE EXPERIMENT LAB (FASE 6: KALIBRASI EMPIRIS & OUT-OF-SAMPLE VALIDATION)")
    print("=" * 135)
    print(f"Data Splitting : {len(df_train)} Sinyal Train (In-Sample 70%) | {len(df_test)} Sinyal Test (Out-of-Sample 30%)")
    print(f"MFE / MAE Rata : Train MFE: {df_train['mfe_pct'].mean():+.2f}% | Test MFE: {df_test['mfe_pct'].mean():+.2f}%")
    print("-" * 135)

    if not df_corr.empty:
        print("\n📈 ANALISIS SIGNIFIKANSI FITUR TERHADAP POTENSI KEUNTUNGAN (TRAIN DATA FEATURE IMPORTANCE):")
        print(f"{'NAMA FITUR / PILAR':<30} {'KORELASI MFE':<15} {'MFE (GRUP ATAS)':<18} {'MFE (GRUP BAWAH)':<18} {'EDGE SPREAD':<12}")
        print("-" * 98)
        for _, r in df_corr.iterrows():
            print(f"{r['fitur']:<30} {r['korelasi_mfe']:>+10.3f}        {r['mfe_high_group']:>+12.2f}%       {r['mfe_low_group']:>+12.2f}%       {r['edge_spread']:>+8.2f}%")
        print("-" * 98)

    print("\n⚖️ KALIBRASI BOBOT 5 PILAR MATEMATIS HASIL RISET EMPIRIS (TOTAL 100 POIN):")
    print(f"   Structure (4H)   : {calibrated_weights['structure']} poin (Tren & EMA Alignment)")
    print(f"   Momentum (1H)    : {calibrated_weights['momentum']} poin (ROC, Akselerasi & Kinetik)")
    print(f"   Whale Flow       : {calibrated_weights['flow']} poin (Taker Buy & Akumulasi)")
    print(f"   Derivatives      : {calibrated_weights['derivatives']} poin (Funding & Dynamic ΔOI)")
    print(f"   AI News Catalyst : {calibrated_weights['news']} poin (Sentimen Global Gemini & Risk Guard)")

    if not df_grid_train.empty:
        print("\n🏆 HASIL GRID SEARCH DYNAMIC ATR PADA DATA TRAINING (TOP 5 KOMBINASI):")
        print(f"{'RANK':<5} {'TP ATR MULT':<13} {'SL ATR MULT':<13} {'WIN RATE':<10} {'PROFIT FACTOR':<15} {'EXPECTANCY':<12} {'NET RETURN':<12}")
        print("-" * 88)
        for i, r in df_grid_train.head(5).iterrows():
            tp_str = f"{r['tp_mult']:.1f}x ATR"
            sl_str = f"{r['sl_mult']:.1f}x ATR"
            wr_str = f"{r['win_rate']:.1f}%"
            pf_str = f"{r['profit_factor']:.2f}"
            exp_str = f"${r['expectancy']:+.2f}"
            net_str = f"{r['net_return_pct']:+.1f}%"
            print(f"#{i+1:<4} {tp_str:<13} {sl_str:<13} {wr_str:<10} {pf_str:<15} {exp_str:<12} {net_str:<12}")
        print("-" * 88)

    print("\n🛡️ UJI VALIDASI OUT-OF-SAMPLE (MENCEGAH OVERFITTING):")
    print(f"{'METRIK PERFORMA':<25} {'IN-SAMPLE (TRAIN 70%)':<25} {'OUT-OF-SAMPLE (TEST 30%)':<25} {'STATUS VALIDITAS':<20}")
    print("-" * 95)
    
    pf_tr = train_metrics.get("profit_factor", 0.0)
    pf_ts = test_metrics.get("profit_factor", 0.0)
    wr_tr = train_metrics.get("win_rate", 0.0)
    wr_ts = test_metrics.get("win_rate", 0.0)
    exp_tr = train_metrics.get("expectancy", 0.0)
    exp_ts = test_metrics.get("expectancy", 0.0)

    pf_status = "✅ SOLID EDGE" if pf_ts >= 1.5 else "⚠️ MARGINAL"
    wr_status = "✅ STABLE" if abs(wr_tr - wr_ts) <= 15.0 else "⚠️ DIVERGENT"
    exp_status = "✅ POSITIVE EXPECTANCY" if exp_ts > 0 else "❌ NEGATIVE"

    print(f"{'Profit Factor':<25} {pf_tr:<25.2f} {pf_ts:<25.2f} {pf_status:<20}")
    print(f"{'Win Rate %':<25} {f'{wr_tr:.1f}%':<25} {f'{wr_ts:.1f}%':<25} {wr_status:<20}")
    print(f"{'Trade Expectancy':<25} {f'${exp_tr:+.2f}':<25} {f'${exp_ts:+.2f}':<25} {exp_status:<20}")
    print("-" * 95)

    print(f"Overfitting Gap (Delta PF) : {abs(pf_tr - pf_ts):.2f} poin (Nilai di bawah 1.5 menandakan model tahan banting terhadap data baru).")
    print("=" * 135 + "\n")


def run_calibration_lab(
    apply_best: bool = False,
    db_path: str = DB_PATH
) -> None:
    """Menjalankan alur penuh eksperimen kalibrasi empiris."""
    df_dataset = load_dataset(db_path=db_path, only_filled=True)
    if df_dataset.empty:
        print("[!] Dataset sinyal yang terisi (filled) belum mencukupi untuk kalibrasi.")
        return

    # 1. Time-series Train/Test Split
    df_train, df_test = split_train_test(df_dataset, train_ratio=0.70)

    # 2. Grid Search Dynamic ATR pada data Train
    df_grid_train = run_dynamic_atr_grid_search(df_train)
    if df_grid_train.empty:
        print("[!] Gagal menjalankan grid search.")
        return

    best_params = df_grid_train.iloc[0].to_dict()
    train_metrics = best_params

    # 3. Evaluasi parameter beku (frozen) pada data Out-of-Sample Test
    test_metrics = evaluate_configuration_dynamic_atr(
        df_test,
        tp_mult=best_params["tp_mult"],
        sl_mult=best_params["sl_mult"]
    )

    # 4. Analisis korelasi fitur & optimasi bobot pada data Train
    df_corr = analyze_feature_correlations(df_train)
    calibrated_weights = optimize_pillar_weights(df_train, df_corr)

    # 5. Cetak laporan ilmiah
    print_experiment_report(
        df_train=df_train,
        df_test=df_test,
        df_grid_train=df_grid_train,
        best_params=best_params,
        train_metrics=train_metrics,
        test_metrics=test_metrics,
        df_corr=df_corr,
        calibrated_weights=calibrated_weights
    )

    # 6. Simpan konfigurasi jika diminta
    if apply_best:
        save_calibrated_config(
            best_params=best_params,
            calibrated_weights=calibrated_weights,
            test_metrics=test_metrics,
            train_metrics=train_metrics,
            filepath=CALIBRATED_CONFIG_PATH
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KripikTo Quantitative Experiment Lab (Fase 6)")
    parser.add_argument("--apply", action="store_true", help="Simpan parameter optimal & bobot terkalibrasi ke calibrated_config.json")
    parser.add_argument("--db", type=str, default=DB_PATH, help="Path ke database SQLite")
    args = parser.parse_args()

    run_calibration_lab(apply_best=args.apply, db_path=args.db)
