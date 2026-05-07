#!/usr/bin/env python3
"""
Energy regression analysis for Spring PetClinic REST.

Reads CodeCarbon results, compares against a statistically-averaged baseline
(mean ± σ over N runs), and exits with a non-zero code when a regression
is detected.

Usage:
  python energy_regression.py                              # compare and report
  python energy_regression.py --update-baseline --runs 3  # recompute baseline from last 3 runs
  python energy_regression.py --output-md report.md       # also write markdown report to file
"""

import argparse
import csv
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASELINE_FILE = Path(__file__).parent / "codecarbon-baseline.json"
DEFAULT_RESULTS_DIR = Path(__file__).parent / "codecarbon-results"

# Mode → dedicated history CSV written by measure_energy.py
TRACKED_MODES: dict[str, str] = {
    "petclinic-tests": "tests_history.csv",
    "petclinic-startup": "startup_history.csv",
}

DEFAULT_WARN_THRESHOLD = 0.10   # +10 % → warning
DEFAULT_FAIL_THRESHOLD = 0.25   # +25 % → fail
DEFAULT_RUNS = 3


# ---------------------------------------------------------------------------
# CSV helpers — mode-level
# ---------------------------------------------------------------------------


def _parse_mode_row(row: dict) -> dict:
    return {
        "emissions": float(row.get("emissions") or 0),
        "energy_consumed": float(row.get("energy_consumed") or 0),
        "duration": float(row.get("duration") or 0),
    }


def read_last_n_results(results_dir: Path, n: int) -> dict[str, list[dict]]:
    """Return {project_name: [last-N rows]} from each mode's history CSV."""
    out: dict[str, list[dict]] = {}
    for project_name, csv_filename in TRACKED_MODES.items():
        csv_path = results_dir / csv_filename
        if not csv_path.exists():
            continue
        rows = []
        with open(csv_path, newline="") as fh:
            for row in csv.DictReader(fh):
                rows.append(_parse_mode_row(row))
        if rows:
            out[project_name] = rows[-n:]
    return out


def read_single_latest(results_dir: Path) -> dict[str, dict]:
    return {
        name: rows[-1]
        for name, rows in read_last_n_results(results_dir, 1).items()
    }


# ---------------------------------------------------------------------------
# CSV helpers — endpoint-level
# ---------------------------------------------------------------------------

ENDPOINTS_HISTORY_FILE = "endpoints_history.csv"


def _parse_endpoint_row(row: dict) -> dict:
    return {
        "endpoint_name": row.get("endpoint_name", ""),
        "path": row.get("path", ""),
        "requests": int(row.get("requests") or 0),
        "emissions": float(row.get("emissions") or 0),
        "energy_consumed": float(row.get("energy_consumed") or 0),
        "duration": float(row.get("duration") or 0),
        "energy_per_request": float(row.get("energy_per_request") or 0),
    }


def read_last_n_endpoint_results(results_dir: Path, n: int) -> dict[str, list[dict]]:
    """Return {endpoint_name: [last-N rows]} from endpoints_history.csv."""
    csv_path = results_dir / ENDPOINTS_HISTORY_FILE
    if not csv_path.exists():
        return {}

    # Collect all rows per endpoint name, preserving order
    all_rows: dict[str, list[dict]] = {}
    with open(csv_path, newline="") as fh:
        for row in csv.DictReader(fh):
            parsed = _parse_endpoint_row(row)
            name = parsed["endpoint_name"]
            all_rows.setdefault(name, []).append(parsed)

    return {name: rows[-n:] for name, rows in all_rows.items()}


def read_latest_endpoint_results(results_dir: Path) -> dict[str, dict]:
    return {
        name: rows[-1]
        for name, rows in read_last_n_endpoint_results(results_dir, 1).items()
    }


# ---------------------------------------------------------------------------
# Baseline helpers
# ---------------------------------------------------------------------------


def _compute_stats(values: list[float]) -> tuple[float, float]:
    mean = statistics.mean(values)
    std_dev = statistics.stdev(values) if len(values) > 1 else 0.0
    return mean, std_dev


def load_baseline() -> dict:
    if BASELINE_FILE.exists():
        return json.loads(BASELINE_FILE.read_text())
    return {}


def save_baseline(data: dict) -> None:
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    BASELINE_FILE.write_text(json.dumps(data, indent=2) + "\n")
    print(f"[regression] Baseline saved → {BASELINE_FILE}", flush=True)


def _baseline_mean(entry: dict) -> float:
    """Supports both new {mean} and legacy {emissions} format."""
    return entry.get("mean") or entry.get("emissions", 0.0)


def _baseline_std(entry: dict) -> float:
    return entry.get("std_dev", 0.0)


def _baseline_n(entry: dict) -> int:
    return entry.get("n", 1)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def status_icon(status: str) -> str:
    return {"pass": "✅", "warn": "⚠️", "fail": "❌", "new": "🆕", "missing": "❓"}.get(status, "")


def classify(current: float, mean: float, warn: float, fail: float) -> tuple[str, float]:
    if mean == 0:
        return "pass", 0.0
    delta = (current - mean) / mean
    if delta > fail:
        return "fail", delta
    if delta > warn:
        return "warn", delta
    return "pass", delta


# ---------------------------------------------------------------------------
# Baseline update
# ---------------------------------------------------------------------------


def update_baseline(results_dir: Path, runs: int) -> int:
    baseline = load_baseline()

    # --- mode-level ---
    mode_rows = read_last_n_results(results_dir, runs)
    for project_name, rows in mode_rows.items():
        em_vals = [r["emissions"] for r in rows]
        en_vals = [r["energy_consumed"] for r in rows]
        em_mean, em_std = _compute_stats(em_vals)
        en_mean, en_std = _compute_stats(en_vals)
        baseline[project_name] = {
            "mean": em_mean,
            "std_dev": em_std,
            "n": len(rows),
            "energy_consumed_mean": en_mean,
            "energy_consumed_std_dev": en_std,
        }
        short = project_name.replace("petclinic-", "")
        print(f"[regression] {short:20s} mean={em_mean:.7f} kg  σ={em_std:.7f}  (n={len(rows)})", flush=True)

    # --- endpoint-level ---
    ep_rows = read_last_n_endpoint_results(results_dir, runs)
    if ep_rows:
        baseline.setdefault("endpoints", {})
        for ep_name, rows in ep_rows.items():
            epr_vals = [r["energy_per_request"] for r in rows]
            em_vals = [r["emissions"] for r in rows]
            epr_mean, epr_std = _compute_stats(epr_vals)
            em_mean, em_std = _compute_stats(em_vals)
            baseline["endpoints"][ep_name] = {
                "mean_epr": epr_mean,       # energy per request (kWh) — primary regression metric
                "std_dev_epr": epr_std,
                "mean_emissions": em_mean,
                "n": len(rows),
                "path": rows[-1]["path"],
            }
            print(f"[regression]   {ep_name:25s} epr={epr_mean:.3e} kWh  σ={epr_std:.3e}  (n={len(rows)})", flush=True)

    if not mode_rows and not ep_rows:
        print("[regression] No history CSVs found — run measure_energy.py first.", file=sys.stderr, flush=True)
        return 1

    save_baseline(baseline)
    return 0


# ---------------------------------------------------------------------------
# Regression comparison
# ---------------------------------------------------------------------------


def _build_mode_rows(current: dict, baseline: dict, warn_pct: float, fail_pct: float):
    """Return (table_rows, overall_status) for mode-level comparison."""
    rows = []
    overall = "pass"

    for mode in TRACKED_MODES:
        short = mode.replace("petclinic-", "")

        if mode not in current:
            rows.append((short, "N/A", "—", "— (not run)", "—", "missing"))
            continue

        cur_em = current[mode]["emissions"]
        cur_en = current[mode]["energy_consumed"]

        if mode not in baseline:
            rows.append((short, f"{cur_em:.7f}", f"{cur_en:.7f}", "no baseline", "—", "new"))
            continue

        entry = baseline[mode]
        b_mean = _baseline_mean(entry)
        b_std = _baseline_std(entry)
        b_n = _baseline_n(entry)

        status, delta = classify(cur_em, b_mean, warn_pct, fail_pct)
        sign = "+" if delta >= 0 else ""
        rows.append((
            short,
            f"{cur_em:.7f}",
            f"{cur_en:.7f}",
            f"{b_mean:.7f} ± {b_std:.7f} (n={b_n})",
            f"{sign}{delta * 100:.1f}%",
            status,
        ))

        if status == "fail":
            overall = "fail"
        elif status == "warn" and overall == "pass":
            overall = "warn"

    return rows, overall


def _build_endpoint_rows(current_eps: dict, baseline: dict, warn_pct: float, fail_pct: float):
    """Return (table_rows, overall_status) for endpoint-level comparison."""
    rows = []
    overall = "pass"
    ep_baseline = baseline.get("endpoints", {})

    for ep in current_eps.values():
        name = ep["endpoint_name"]
        cur_epr = ep["energy_per_request"]
        cur_em = ep["emissions"]
        path = ep["path"]

        if name not in ep_baseline:
            rows.append((name, path, f"{cur_epr:.3e}", f"{cur_em:.7f}", "no baseline", "—", "new"))
            continue

        entry = ep_baseline[name]
        b_mean = entry["mean_epr"]
        b_std = entry.get("std_dev_epr", 0.0)
        b_n = entry.get("n", 1)

        status, delta = classify(cur_epr, b_mean, warn_pct, fail_pct)
        sign = "+" if delta >= 0 else ""
        rows.append((
            name,
            path,
            f"{cur_epr:.3e}",
            f"{cur_em:.7f}",
            f"{b_mean:.3e} ± {b_std:.3e} (n={b_n})",
            f"{sign}{delta * 100:.1f}%",
            status,
        ))

        if status == "fail":
            overall = "fail"
        elif status == "warn" and overall == "pass":
            overall = "warn"

    return rows, overall


def run_comparison(args: argparse.Namespace) -> int:
    results_dir = Path(args.results_dir)
    current_modes = read_single_latest(results_dir)
    current_eps = read_latest_endpoint_results(results_dir)

    if not current_modes and not current_eps:
        print(
            f"[regression] No history CSVs found in {results_dir} — run measure_energy.py first.",
            file=sys.stderr, flush=True,
        )
        return 1

    baseline = load_baseline()
    warn_pct = args.warn_threshold
    fail_pct = args.fail_threshold

    lines = ["## ⚡ Energy Regression Report\n"]

    # ---- Mode-level table ---------------------------------------------------
    if current_modes:
        mode_rows, mode_overall = _build_mode_rows(current_modes, baseline, warn_pct, fail_pct)
        lines += [
            "### Build-level",
            "| Mode | Emissions (kg CO₂eq) | Energy (kWh) | Baseline mean ± σ (kg CO₂eq) | Δ vs mean | Status |",
            "|---|---|---|---|---|---|",
        ]
        for short, em, en, b_cell, delta, status in mode_rows:
            lines.append(f"| `{short}` | {em} | {en} | {b_cell} | {delta} | {status_icon(status)} {status.upper()} |")
    else:
        mode_overall = "pass"

    # ---- Endpoint-level table -----------------------------------------------
    if current_eps:
        ep_rows, ep_overall = _build_endpoint_rows(current_eps, baseline, warn_pct, fail_pct)
        lines += [
            "\n### Per-endpoint (energy per request)",
            "| Endpoint | Path | Energy/req (kWh) | Emissions (kg CO₂eq) | Baseline mean ± σ (kWh/req) | Δ vs mean | Status |",
            "|---|---|---|---|---|---|---|",
        ]
        for name, path, epr, em, b_cell, delta, status in ep_rows:
            lines.append(f"| `{name}` | `{path}` | {epr} | {em} | {b_cell} | {delta} | {status_icon(status)} {status.upper()} |")
    else:
        ep_overall = "pass"

    # ---- Summary ------------------------------------------------------------
    overall = "fail" if "fail" in (mode_overall, ep_overall) else \
              "warn" if "warn" in (mode_overall, ep_overall) else "pass"

    lines.append(
        f"\n**Thresholds:** warn ≥ +{warn_pct*100:.0f}%, fail ≥ +{fail_pct*100:.0f}%  \n"
        f"**Overall result:** {status_icon(overall)} **{overall.upper()}**"
    )

    report = "\n".join(lines)
    print(report, flush=True)

    if args.output_md:
        Path(args.output_md).write_text(report + "\n")
        print(f"[regression] Report written → {args.output_md}", flush=True)

    return 1 if overall == "fail" else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Energy regression analysis using CodeCarbon results.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--results-dir",
        default=str(DEFAULT_RESULTS_DIR),
        help="Directory containing history CSVs (default: ./codecarbon-results)",
    )
    parser.add_argument(
        "--warn-threshold",
        type=float,
        default=DEFAULT_WARN_THRESHOLD,
        help="Fraction increase that triggers a warning (default: 0.10 = 10%%)",
    )
    parser.add_argument(
        "--fail-threshold",
        type=float,
        default=DEFAULT_FAIL_THRESHOLD,
        help="Fraction increase that fails the check (default: 0.25 = 25%%)",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Compute and store a new baseline from the last --runs measurements",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=DEFAULT_RUNS,
        help=f"Number of recent runs to average when capturing a baseline (default: {DEFAULT_RUNS})",
    )
    parser.add_argument(
        "--output-md",
        default=None,
        metavar="FILE",
        help="Also write the markdown report to this file",
    )

    args = parser.parse_args()

    if args.update_baseline:
        sys.exit(update_baseline(Path(args.results_dir), args.runs))
    else:
        sys.exit(run_comparison(args))


if __name__ == "__main__":
    main()
