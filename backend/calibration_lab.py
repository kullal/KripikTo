"""Chronological TP/SL experiments using cached, complete forward candle paths.

Scoring and trade plans share helpers with the scanner. Train/test boundaries group
scan timestamps and purge overlapping labels. Exit grid search keeps baseline
weights fixed; joint weight/exit optimization is deliberately not auto-promoted.
Metrics are per-trade net returns, not a capital-constrained portfolio backtest.
"""

import sys
import json
import sqlite3
import argparse
from pathlib import Path
from typing import Dict, List, Any, Tuple, Optional
import pandas as pd
import numpy as np
from contextlib import closing
from backend.scoring import SCORING_VERSION, PILLAR_COLUMNS, BASE_WEIGHTS, score_snapshot
from backend.replay import REPLAY_VERSION, Costs, replay_trade, summarize, timestamp_ms, validate_candles
from backend.replay_store import load_candles
from backend.feature_engine import calculate_dynamic_tp_sl

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
    """Only versioned, technically executable snapshots with complete forward paths.

    only_filled is retained for API compatibility; unfilled orders remain in the cohort.
    """
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        columns = {r[1] for r in conn.execute("PRAGMA table_info(scan_results)")}
        if not {"scoring_version", "entry_status", "data_quality_status"} <= columns:
            return pd.DataFrame()
        rows = conn.execute("""SELECT * FROM scan_results WHERE scoring_version=?
            AND entry_status='TRIGGERED' AND data_quality_status='OK'
            ORDER BY scan_time, symbol""", (SCORING_VERSION,)).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            if record.get("execution_eligible") == 0:
                continue
            start = timestamp_ms(record["scan_time"])
            candles = load_candles(conn, record["symbol"], start, start + 49 * 3_600_000)
            first = ((start + 899999) // 900000) * 900000
            # Fixed fill (2h) plus holding (6h) window, including deadline open.
            if not candles or candles[0]["open_time"] != first or candles[-1]["open_time"] < first + 8 * 3_600_000:
                continue
            try:
                validate_candles(candles, 900000)
            except ValueError:
                continue
            record["forward_candles"] = candles
            baseline = replay_trade(record, candles)
            record.update({k: baseline[k] for k in ("result", "mfe_pct", "mae_pct", "net_return_pct")})
            records.append(record)
    return pd.DataFrame(records)


def split_train_test(df: pd.DataFrame, train_ratio: float = 0.70) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Split whole scan timestamps; purge overlapping eight-hour label windows."""
    if df.empty or len(df) < 30 or not 0 < train_ratio < 1:
        return pd.DataFrame(), pd.DataFrame()
    ordered = df.sort_values("scan_time").copy()
    cutoff = pd.to_datetime(ordered.iloc[int(len(ordered) * train_ratio)]["scan_time"], utc=True)
    times = pd.to_datetime(ordered["scan_time"], utc=True)
    train = ordered[times + pd.Timedelta(hours=8.25) < cutoff].copy()
    test = ordered[times >= cutoff].copy()
    if len(train) < 20 or len(test) < 10:
        return pd.DataFrame(), pd.DataFrame()
    return train.reset_index(drop=True), test.reset_index(drop=True)


def simulate_trade_outcome(*args, **kwargs):
    """MFE/MAE cannot reconstruct ordering or a path past an earlier exit."""
    raise ValueError("Aggregate MFE/MAE replay retired; use replay_trade with forward candles")


def evaluate_configuration_dynamic_atr(df: pd.DataFrame, tp_mult: float, sl_mult: float) -> Dict[str, Any]:
    results = []
    cfg = {"tp_atr_multiplier": tp_mult, "sl_atr_multiplier": sl_mult,
           "target_profit_1_pct": 4.5, "stop_loss_pct": -4.8}
    for _, row in df.iterrows():
        signal = row.to_dict()
        candles = signal.get("forward_candles")
        if not isinstance(candles, list) or not candles:
            continue
        entry = float(signal["entry_price"])
        atr = float(signal["atr14"])
        support = float(signal.get("support_level", float("nan")))
        if not all(np.isfinite(x) and x > 0 for x in (entry, atr, support)):
            continue
        dec = 8 if entry < 0.01 else 6 if entry < 1 else 4
        signal.update(calculate_dynamic_tp_sl(entry, support, atr, dec, config=cfg))
        results.append(replay_trade(signal, candles, costs=Costs()))
    stats = summarize(results)
    if not stats["completed"]:
        return {}
    n = stats["completed"]
    return {
        "tp_mult": tp_mult, "sl_mult": sl_mult, "total_trades": n,
        "total_signals": len(results), "win_rate": stats["win_rate"],
        "loss_rate": 100 * sum(r.get("net_return_pct") is not None and r["net_return_pct"] < 0 for r in results) / n,
        "timeout_rate": 100 * sum(r["result"] == "TIMEOUT" for r in results) / n,
        "ambiguous_count": stats["ambiguous"],
        "profit_factor": stats["profit_factor"],
        "expectancy": stats["expectancy_pct"], "net_return_pct": stats["sum_trade_return_pct"],
        "replay_version": REPLAY_VERSION,
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
            if metrics and metrics["profit_factor"] is not None:
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


def _evaluate_weight_set(df: pd.DataFrame, weights: Dict[str, int]) -> Dict[str, float]:
    if df.empty or any(col not in df.columns for col in PILLAR_COLUMNS.values()):
        return {}
    x = df.copy()
    x["_calibrated_score"] = [score_snapshot(r, weights)[0] for r in x.to_dict(orient="records")]
    cutoff = x["_calibrated_score"].quantile(0.70)
    selected = x[x["_calibrated_score"] >= cutoff]
    returns = pd.to_numeric(selected["net_return_pct"], errors="coerce").dropna()
    if returns.empty:
        return {}
    return {"objective": float(returns.mean()), "success_rate": float((returns > 0).mean() * 100),
            "timeout_rate": float((selected["result"] == "TIMEOUT").mean() * 100),
            "cutoff": float(cutoff), "selected_count": len(returns)}


def optimize_pillar_weights(df_train: pd.DataFrame, df_corr: pd.DataFrame) -> Dict[str, int]:
    """
    Empirically test candidate weights on training outcomes.
    News stays at 10 until historical news scores are available in snapshots.
    """
    if len(df_train) < 20:
        return {"structure": 25, "momentum": 30, "flow": 20, "derivatives": 15, "news": 10}

    best = None
    for structure in range(15, 36, 5):
        for momentum in range(20, 41, 5):
            for flow in range(10, 26, 5):
                derivatives = 90 - structure - momentum - flow
                if not 10 <= derivatives <= 25:
                    continue
                weights = {
                    "structure": structure,
                    "momentum": momentum,
                    "flow": flow,
                    "derivatives": derivatives,
                    "news": 10
                }
                metrics = _evaluate_weight_set(df_train, weights)
                if not metrics:
                    continue
                key = (metrics["objective"], metrics["success_rate"], -metrics["timeout_rate"])
                if best is None or key > best[0]:
                    best = (key, weights)

    if best is None:
        return {"structure": 25, "momentum": 30, "flow": 20, "derivatives": 15, "news": 10}
    return {k: int(v) for k, v in best[1].items()}


def save_calibrated_config(
    best_params: Dict[str, Any],
    calibrated_weights: Dict[str, int],
    test_metrics: Dict[str, Any],
    train_metrics: Dict[str, Any],
    filepath: str = CALIBRATED_CONFIG_PATH
) -> None:
    """Menyimpan konfigurasi parameter optimal hasil kalibrasi out-of-sample ke JSON."""
    pf = test_metrics.get("profit_factor")
    if (test_metrics.get("replay_version") != REPLAY_VERSION or
            test_metrics.get("total_trades", 0) < 10 or
            pf is None or not np.isfinite(pf) or pf <= 1 or test_metrics.get("expectancy", 0) <= 0):
        raise ValueError("Config rejected: need >=10 completed OOS replays, finite PF>1 and net expectancy>0")
    config_payload = {
        "scoring_version": SCORING_VERSION, "replay_version": REPLAY_VERSION, "validated": True,
        "fee_bps_per_side": 10.0, "slippage_bps_per_side": 5.0,
        "calibrated_at": str(pd.Timestamp.now(tz="UTC")),
        "validation_method": "Time-Series Train/Test Split (70% In-Sample / 30% Out-of-Sample)",
        "train_sample_trades": train_metrics.get("total_trades", 0),
        "test_sample_trades": test_metrics.get("total_trades", 0),
        "minimum_oos_sample_required": 10,
        "tp_atr_multiplier": best_params.get("tp_mult", 1.8),
        "sl_atr_multiplier": best_params.get("sl_mult", 1.4),
        "target_profit_1_pct": 4.5,
        "stop_loss_pct": -4.8,
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

    print("\n⚖️ BOBOT BASELINE TETAP (EKSPERIMEN INI HANYA MENGUJI TP/SL):")
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
            exp_str = f"{r['expectancy']:+.2f}%"
            net_str = f"{r['net_return_pct']:+.1f}%"
            print(f"#{i+1:<4} {tp_str:<13} {sl_str:<13} {wr_str:<10} {pf_str:<15} {exp_str:<12} {net_str:<12}")
        print("-" * 88)

    print("\n🛡️ UJI VALIDASI OUT-OF-SAMPLE (MENCEGAH OVERFITTING):")
    print(f"{'METRIK PERFORMA':<25} {'IN-SAMPLE (TRAIN 70%)':<25} {'OUT-OF-SAMPLE (TEST 30%)':<25} {'STATUS VALIDITAS':<20}")
    print("-" * 95)
    
    pf_tr = train_metrics.get("profit_factor") or 0.0
    pf_ts = test_metrics.get("profit_factor") or 0.0
    wr_tr = train_metrics.get("win_rate", 0.0)
    wr_ts = test_metrics.get("win_rate", 0.0)
    exp_tr = train_metrics.get("expectancy", 0.0)
    exp_ts = test_metrics.get("expectancy", 0.0)

    pf_status = "Observed PF > 1" if pf_ts > 1 else "Belum lolos / N/A"
    wr_status = "✅ STABLE" if abs(wr_tr - wr_ts) <= 15.0 else "⚠️ DIVERGENT"
    exp_status = "✅ POSITIVE EXPECTANCY" if exp_ts > 0 else "❌ NEGATIVE"

    print(f"{'Profit Factor':<25} {pf_tr:<25.2f} {pf_ts:<25.2f} {pf_status:<20}")
    print(f"{'Win Rate %':<25} {f'{wr_tr:.1f}%':<25} {f'{wr_ts:.1f}%':<25} {wr_status:<20}")
    print(f"{'Expectancy % per trade':<25} {f'{exp_tr:+.2f}%':<25} {f'{exp_ts:+.2f}%':<25} {exp_status:<20}")
    print("-" * 95)

    print(f"Selisih PF: {abs(pf_tr - pf_ts):.2f}; bukan bukti bebas overfitting. PF tanpa loss dilaporkan N/A pada hasil mentah.")
    print("=" * 135 + "\n")


def run_calibration_lab(
    apply_best: bool = False,
    db_path: str = DB_PATH
) -> None:
    """Menjalankan alur penuh eksperimen kalibrasi empiris."""
    df_dataset = load_dataset(db_path=db_path, only_filled=True)
    if df_dataset.empty:
        print("[!] Belum ada snapshot scoring terbaru dengan forward candle lengkap untuk kalibrasi.")
        return

    # 1. Time-series Train/Test Split
    df_train, df_test = split_train_test(df_dataset, train_ratio=0.70)
    if df_train.empty or df_test.empty:
        print(f"[!] Dataset belum cukup setelah pemisahan waktu dan purge: {len(df_dataset)} kandidat; perlu >=20 train dan >=10 test.")
        return

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
    # Tune exits only in this experiment. Joint weight/exit searches need an
    # independent validation stage; do not promote weights fitted to other exits.
    calibrated_weights = BASE_WEIGHTS.copy()

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
        try:
            save_calibrated_config(
                best_params=best_params,
                calibrated_weights=calibrated_weights,
                test_metrics=test_metrics,
                train_metrics=train_metrics,
                filepath=CALIBRATED_CONFIG_PATH
            )
        except ValueError as exc:
            print(f"[!] {exc}; konfigurasi aktif tidak diganti.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KripikTo Quantitative Experiment Lab (Fase 6)")
    parser.add_argument("--apply", action="store_true", help="Simpan parameter optimal & bobot terkalibrasi ke calibrated_config.json")
    parser.add_argument("--db", type=str, default=DB_PATH, help="Path ke database SQLite")
    args = parser.parse_args()

    run_calibration_lab(apply_best=args.apply, db_path=args.db)
