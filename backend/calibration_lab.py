"""Chronological candle replay, purged validation and finite-capital experiments.

Legacy MFE/MAE summaries are diagnostics, never counterfactual execution evidence.
"""
import argparse
import datetime
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pandas as pd

try:
    from backend.feature_engine import BASE_WEIGHTS, SCORING_VERSION, compose_quant_score, calculate_dynamic_tp_sl
    from backend.outcome_tracker import init_outcome_tracker_db, evaluate_single_signal
    from backend.research_utils import database, DEFAULT_COSTS, FORWARD_HOURS, utc_time, read_forward_candles, complete_forward_window, portfolio_metrics
except ImportError:
    from feature_engine import BASE_WEIGHTS, SCORING_VERSION, compose_quant_score, calculate_dynamic_tp_sl
    from outcome_tracker import init_outcome_tracker_db, evaluate_single_signal
    from research_utils import database, DEFAULT_COSTS, FORWARD_HOURS, utc_time, read_forward_candles, complete_forward_window, portfolio_metrics

DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = str(DATA_DIR / "kripto.db")
CALIBRATED_CONFIG_PATH = str(DATA_DIR / "calibrated_config.json")
PLAN_DEFAULTS = {"target_profit_1_pct": 4.5, "stop_loss_pct": -4.8,
                 "tp_atr_multiplier": 1.6, "sl_atr_multiplier": 1.25}
MIN_TRAIN, MIN_EVALUATION = 20, 10


def load_dataset(db_path=DB_PATH, only_filled=False):
    """Use canonical, quality-checked triggered scans with complete forward candles."""
    try:
        from backend.scanner import init_scanner_db
    except ImportError:
        from scanner import init_scanner_db
    init_scanner_db(db_path)
    init_outcome_tracker_db(db_path)
    with database(db_path) as conn:
        df = pd.read_sql("""
            SELECT s.*, o.forward_complete FROM scan_results s
            JOIN signal_outcomes o ON o.scan_time=s.scan_time AND o.symbol=s.symbol
            WHERE s.entry_status='TRIGGERED' AND s.data_quality_status='OK'
            AND s.scoring_version=? ORDER BY s.scan_time, s.symbol
        """, conn, params=(SCORING_VERSION,))
        paths = []
        for row in df.to_dict("records"):
            candles = read_forward_candles(conn, row["scan_time"], row["symbol"])
            paths.append(candles if complete_forward_window(row["scan_time"], candles) else None)
    df["forward_candles"] = paths
    df = df[df.forward_candles.notna()].copy()
    df["label_end"] = pd.to_datetime(df.scan_time, utc=True) + pd.Timedelta(hours=FORWARD_HOURS + 1)
    if only_filled:
        df = df[df.apply(lambda r: evaluate_single_signal(r.to_dict(), candles=r.forward_candles)["is_filled"] == 1, axis=1)].copy()
    return df.reset_index(drop=True)


def _purge_before(frame, boundary):
    label_end = pd.to_datetime(frame["label_end"], utc=True) if "label_end" in frame else pd.to_datetime(frame.scan_time, utc=True) + pd.Timedelta(hours=FORWARD_HOURS + 1)
    return frame[label_end < boundary].copy().reset_index(drop=True)


def split_train_test(df, train_ratio=0.70):
    """Keep a timestamp group together and purge overlapping forward labels."""
    if df.empty or not 0 < train_ratio < 1:
        return pd.DataFrame(), pd.DataFrame()
    times = pd.to_datetime(df.scan_time, utc=True)
    groups = sorted(times.unique())
    index = int(len(groups) * train_ratio)
    if index < 1 or index >= len(groups):
        return pd.DataFrame(), pd.DataFrame()
    boundary = groups[index]
    train = _purge_before(df[times < boundary], boundary)
    test = df[times >= boundary].copy().reset_index(drop=True)
    if len(train) < MIN_TRAIN or len(test) < MIN_EVALUATION:
        return pd.DataFrame(), pd.DataFrame()
    return train, test


def split_train_validation_test(df):
    train, rest = split_train_test(df, 0.60)
    if train.empty:
        return train, pd.DataFrame(), pd.DataFrame()
    # Validation/test both require ten records, with complete timestamp groups.
    times = pd.to_datetime(rest.scan_time, utc=True)
    groups = sorted(times.unique())
    if len(groups) < 2:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    boundary = groups[len(groups) // 2]
    validation = _purge_before(rest[times < boundary], boundary)
    test = rest[times >= boundary].copy().reset_index(drop=True)
    if len(validation) < MIN_EVALUATION or len(test) < MIN_EVALUATION:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    return train, validation, test


def walk_forward_splits(df, folds=3):
    """Expanding training windows with disjoint, purged chronological tests."""
    if df.empty:
        return []
    times = pd.to_datetime(df.scan_time, utc=True)
    groups = sorted(times.unique())
    first = len(groups) // 2
    boundaries = [first + (len(groups) - first) * i // folds for i in range(folds + 1)]
    result = []
    for left, right in zip(boundaries, boundaries[1:]):
        if left >= right or left >= len(groups):
            continue
        boundary = groups[left]
        train = _purge_before(df[times < boundary], boundary)
        mask = times >= boundary
        if right < len(groups):
            mask &= times < groups[right]
        test = df[mask].copy()
        if len(train) >= MIN_TRAIN and len(test) >= MIN_EVALUATION:
            result.append((train, test))
    return result


def simulate_trade_outcome(mfe_pct, mae_pct, target_tp_pct, stop_loss_pct,
                           actual_result=None, ret_12h_pct=None):
    """Compatibility guard: extrema cannot identify counterfactual trade outcomes."""
    raise ValueError("MFE/MAE cannot reconstruct execution order; use forward candle replay.")


def select_candidates(df, weights=None, min_score=40, top_n=15, score_mode="v2"):
    if df.empty:
        return df.copy()
    weights = BASE_WEIGHTS if weights is None else weights
    selected = df.copy()
    if score_mode == "v1":
        selected["selection_score"] = pd.to_numeric(selected.v1_score, errors="coerce")
    else:
        selected["selection_score"] = selected.apply(lambda r: compose_quant_score({
            "structure": r.structure_score, "momentum": r.momentum_score,
            "flow": r.flow_score, "derivatives": r.derivative_score},
            weights, r.get("score_penalty", 0)), axis=1)
    selected = selected[selected.selection_score >= min_score]
    # Same fixed score gate and top-N policy as scanner, within stored candidates.
    selected = selected.sort_values(["scan_time", "selection_score", "taker_buy_ratio", "quote_vol_m"],
                                    ascending=[True, False, False, False])
    return selected.groupby("scan_time", sort=False).head(top_n).copy()


def evaluate_configuration_dynamic_atr(df, tp_mult, sl_mult, weights=None, costs=None,
                                       score_mode="v2", config=None):
    selected = select_candidates(df, weights, score_mode=score_mode)
    if selected.empty:
        return {}
    plan = dict(PLAN_DEFAULTS)
    plan.update(config or {})
    plan.update(tp_atr_multiplier=float(tp_mult), sl_atr_multiplier=float(sl_mult))
    evaluated = []
    for row in selected.to_dict("records"):
        if not isinstance(row.get("forward_candles"), list) or not complete_forward_window(row["scan_time"], row["forward_candles"]):
            raise ValueError("A complete fixed forward candle window is required.")
        entry = float(row["entry_price"])
        support = float(row["support_level"])
        atr = float(row["atr14"])
        dec = 8 if float(row["last_price"]) < 0.01 else (6 if float(row["last_price"]) < 1 else 4)
        targets = calculate_dynamic_tp_sl(entry, support, atr, dec=dec, config=plan)
        signal = dict(row, **targets)
        outcome = evaluate_single_signal(signal, candles=row["forward_candles"], costs=costs)
        if outcome["result"] == "PENDING":
            raise ValueError("Incomplete trade despite a forward window.")
        evaluated.append(outcome)
    portfolio = portfolio_metrics(evaluated)
    filled = [r for r in evaluated if r["is_filled"]]
    resolved = [r for r in filled if r["net_return_pct"] is not None]
    amb = sum(r["result"] == "AMBIGUOUS" for r in evaluated)
    denominator = len(resolved)
    metrics = {
        "tp_mult": tp_mult, "sl_mult": sl_mult, "total_trades": len(selected),
        "filled_trades": len(filled), "fill_rate": len(filled) / len(selected) * 100,
        "win_rate": sum(r["result"] in ("TP1_HIT", "TP2_HIT") for r in resolved) / denominator * 100 if denominator else 0,
        "loss_rate": sum(r["result"] == "SL_HIT" for r in resolved) / denominator * 100 if denominator else 0,
        "timeout_rate": sum(r["result"] == "TIMEOUT" for r in resolved) / denominator * 100 if denominator else 0,
        "ambiguous_count": amb, "resolved_trades": denominator,
        "avg_mfe": sum(r["mfe_pct"] for r in filled) / len(filled) if filled else 0,
        "avg_mae": sum(r["mae_pct"] for r in filled) / len(filled) if filled else 0,
        "costs": DEFAULT_COSTS if costs is None else costs,
        "scope": "stored triggered candidate pool; excludes news; not a full-universe backtest",
        "plan": plan, **portfolio,
    }
    # Unknown executions are never converted into synthetic returns.
    metrics["eligible_for_selection"] = bool(not amb and portfolio["portfolio_complete"] and denominator)
    return metrics


def run_dynamic_atr_grid_search(df, weights=None, costs=None):
    trials = []
    for tp in (1.2, 1.5, 1.8, 2.0, 2.4, 2.8, 3.2):
        for sl in (1.0, 1.2, 1.4, 1.6, 1.8, 2.0):
            metrics = evaluate_configuration_dynamic_atr(df, tp, sl, weights, costs)
            if metrics and metrics["eligible_for_selection"]:
                trials.append(metrics)
    return pd.DataFrame(trials).sort_values(["portfolio_return_pct", "expectancy"], ascending=False).reset_index(drop=True) if trials else pd.DataFrame()


def optimize_pillar_weights(df_train, df_corr=None, plan=None, costs=None):
    best = None
    plan = PLAN_DEFAULTS if plan is None else plan
    for structure in (20, 25, 30):
        for momentum in (25, 30, 35):
            for flow in (15, 20, 25):
                derivative = 90 - structure - momentum - flow
                if not 10 <= derivative <= 25:
                    continue
                weights = dict(structure=structure, momentum=momentum, flow=flow, derivatives=derivative, news=10)
                metrics = evaluate_configuration_dynamic_atr(df_train, plan["tp_atr_multiplier"],
                    plan["sl_atr_multiplier"], weights, costs, config=plan)
                if metrics and metrics["eligible_for_selection"]:
                    key = (metrics["portfolio_return_pct"], metrics["expectancy"])
                    if best is None or key > best[0]:
                        best = (key, weights)
    return best[1] if best else dict(BASE_WEIGHTS)


def _ranges(frames):
    return {name: {"start": frame.scan_time.min(), "end": frame.scan_time.max(), "samples": len(frame)}
            for name, frame in frames.items() if not frame.empty}


def save_calibrated_config(best_params, calibrated_weights, test_metrics, train_metrics,
                           filepath=CALIBRATED_CONFIG_PATH, metadata=None):
    plan = dict(best_params.get("plan", PLAN_DEFAULTS))
    payload = {
        "version": SCORING_VERSION, "calibrated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        **plan, "weights": calibrated_weights,
        "in_sample_performance": train_metrics, "out_of_sample_performance": test_metrics,
        "execution_costs": test_metrics.get("costs", DEFAULT_COSTS),
        "time_stop_hours": 6, "max_positions": 5, "allocation_pct": 20,
        "validation_method": "timestamp-grouped train/validation/test with forward-label purge",
        "research_scope": "stored triggered candidate pool; excludes news",
        **(metadata or {}),
    }
    if not test_metrics.get("eligible_for_selection"):
        raise ValueError("Unknown/out-of-sample outcomes cannot be applied.")
    Path(filepath).parent.mkdir(parents=True, exist_ok=True)
    Path(filepath).write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")


def run_calibration_lab(apply_best=False, db_path=DB_PATH, costs=None):
    dataset = load_dataset(db_path)
    if dataset.empty:
        print("[!] No complete canonical forward windows. Run outcome_tracker --recheck first.")
        return None
    train, validation, test = split_train_validation_test(dataset)
    if train.empty:
        print("[!] Need timestamp groups with at least 20 train / 10 validation / 10 test records after purging overlapping labels.")
        return None
    grid = run_dynamic_atr_grid_search(train, costs=costs)
    if grid.empty:
        print("[!] No fully resolved replay configuration; no parameters applied.")
        return None
    best = grid.iloc[0].to_dict()
    weights = optimize_pillar_weights(train, plan=best["plan"], costs=costs)
    # Validation chooses between baseline and the train proposal. Test stays untouched.
    baseline = evaluate_configuration_dynamic_atr(validation, 1.6, 1.25, BASE_WEIGHTS, costs)
    proposal = evaluate_configuration_dynamic_atr(validation, best["tp_mult"], best["sl_mult"], weights, costs, config=best["plan"])
    use_proposal = bool(proposal and proposal["eligible_for_selection"] and
                        (not baseline or not baseline["eligible_for_selection"] or
                         proposal["portfolio_return_pct"] > baseline["portfolio_return_pct"]))
    if not use_proposal:
        weights = dict(BASE_WEIGHTS)
        best = {"tp_mult": 1.6, "sl_mult": 1.25, "plan": dict(PLAN_DEFAULTS)}
    train_metrics = evaluate_configuration_dynamic_atr(train, best["tp_mult"], best["sl_mult"], weights, costs, config=best["plan"])
    test_metrics = evaluate_configuration_dynamic_atr(test, best["tp_mult"], best["sl_mult"], weights, costs, config=best["plan"])
    ab = {mode: evaluate_configuration_dynamic_atr(test, 1.6, 1.25, BASE_WEIGHTS, costs, score_mode=mode)
          for mode in ("v1", "v2")}
    # Diagnostic walk-forward folds use defaults and only earlier training outcomes.
    walks = []
    for fold_train, fold_test in walk_forward_splits(dataset):
        fold_grid = run_dynamic_atr_grid_search(fold_train, costs=costs)
        if fold_grid.empty:
            continue
        choice = fold_grid.iloc[0].to_dict()
        fold_weights = optimize_pillar_weights(fold_train, plan=choice["plan"], costs=costs)
        walks.append({"ranges": _ranges({"train": fold_train, "test": fold_test}),
            "metrics": evaluate_configuration_dynamic_atr(fold_test, choice["tp_mult"], choice["sl_mult"], fold_weights, costs, config=choice["plan"])})
    report = {"dataset_range": _ranges({"train": train, "validation": validation, "test": test}),
              "weights": weights, "plan": best["plan"], "train": train_metrics,
              "validation": proposal if use_proposal else baseline, "test": test_metrics,
              "ab_same_candidate_pool": ab, "walk_forward": walks,
              "atr_trials": 42, "weight_trials": 27,
              "uncertainty": "No significance/edge claim; correlated trades and search selection require further statistical analysis."}
    print(json.dumps(report, indent=2, allow_nan=False))
    if apply_best:
        save_calibrated_config(best, weights, test_metrics, train_metrics,
            metadata={"dataset_range": report["dataset_range"], "validation_performance": report["validation"],
                      "walk_forward": walks, "ab_same_candidate_pool": ab,
                      "trial_counts": {"atr": 42, "weights_upper_bound": 27}})
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KripikTo chronological replay calibration")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--fee-pct", type=float, default=DEFAULT_COSTS["fee_pct"])
    parser.add_argument("--slippage-pct", type=float, default=DEFAULT_COSTS["slippage_pct"])
    parser.add_argument("--spread-pct", type=float, default=DEFAULT_COSTS["spread_pct"])
    args = parser.parse_args()
    run_calibration_lab(args.apply, args.db,
        costs={"fee_pct": args.fee_pct, "slippage_pct": args.slippage_pct, "spread_pct": args.spread_pct})
