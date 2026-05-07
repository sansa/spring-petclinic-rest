#!/usr/bin/env python3
"""
Energy regression analysis for Spring PetClinic REST.

Reads CodeCarbon results, compares against a statistically-averaged baseline,
and exits with a non-zero code when a regression is detected.

Usage:
  python energy_regression.py                              # compare and report
  python energy_regression.py --update-baseline --runs 5  # recompute baseline from last 5 runs
  python energy_regression.py --output-md report.md       # write markdown report to file
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

# Map project_name → our own fixed-schema history CSV (written by measure_energy.py).
# Using separate files avoids CodeCarbon's schema-drift backups resetting the data.
TRACKED_MODES: dict[str, str] = {
    "petclinic-tests": "tests_history.csv",
    "petclinic-startup": "startup_history.csv",
}

DEFAULT_WARN_THRESHOLD = 0.10   # +10 % → warning
DEFAULT_FAIL_THRESHOLD = 0.25   # +25 % → fail
DEFAULT_RUNS = 3                # runs averaged when capturing a new baseline


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------


def _parse_row(row: dict) -> dict:
    return {
        "emissions": float(row.get("emissions") or 0),
        "energy_consumed": float(row.get("energy_consumed") or 0),
        "duration": float(row.get("duration") or 0),
        "timestamp": row.get("timestamp", ""),
    }


def read_last_n_results(results_dir: Path, n: int) -> dict[str, list[dict]]:
    """Return {project_name: [last-N rows]} from each mode's dedicated CSV."""
    out: dict[str, list[dict]] = {}
    for project_name, csv_filename in TRACKED_MODES.items():
        csv_path = results_dir / csv_filename
        if not csv_path.exists():
            continue
        rows = []
        with open(csv_path, newline="") as fh:
            for row in csv.DictReader(fh):
                rows.append(_parse_row(row))
        if rows:
            out[project_name] = rows[-n:]   # take the most recent n
    return out


def read_single_latest(results_dir: Path) -> dict[str, dict]:
    """Return {project_name: last row} — convenience wrapper for comparison runs."""
    return {
        name: rows[-1]
        for name, rows in read_last_n_results(results_dir, 1).items()
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
    """Read baseline mean — supports both new {mean} and legacy {emissions} format."""
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
    """Return (status, delta_fraction) comparing current against baseline mean."""
    if mean == 0:
        return "pass", 0.0
    delta = (current - mean) / mean
    if delta > fail:
        return "fail", delta
    if delta > warn:
        return "warn", delta
    return "pass", delta


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def update_baseline(results_dir: Path, runs: int) -> int:
    """Compute mean ± σ over the last `runs` rows and persist to baseline file."""
    all_rows = read_last_n_results(results_dir, runs)
    if not all_rows:
        print("[regression] No results found — run measure_energy.py first.", file=sys.stderr, flush=True)
        return 1

    baseline = load_baseline()
    for project_name, rows in all_rows.items():
        actual_n = len(rows)
        em_vals = [r["emissions"] for r in rows]
        en_vals = [r["energy_consumed"] for r in rows]
        em_mean, em_std = _compute_stats(em_vals)
        en_mean, en_std = _compute_stats(en_vals)

        baseline[project_name] = {
            "mean": em_mean,
            "std_dev": em_std,
            "n": actual_n,
            "energy_consumed_mean": en_mean,
            "energy_consumed_std_dev": en_std,
        }
        short = project_name.replace("petclinic-", "")
        print(
            f"[regression] {short}: mean={em_mean:.7f} kg  σ={em_std:.7f}  (n={actual_n})",
            flush=True,
        )

    save_baseline(baseline)
    return 0


def run_comparison(args: argparse.Namespace) -> int:
    results_dir = Path(args.results_dir)
    current = read_single_latest(results_dir)

    if not current:
        print(
            f"[regression] No history CSVs found in {results_dir} — run measure_energy.py first.",
            file=sys.stderr, flush=True,
        )
        return 1

    baseline = load_baseline()
    warn_pct = args.warn_threshold
    fail_pct = args.fail_threshold

    # ---- Build report rows --------------------------------------------------
    table_rows = []
    overall = "pass"

    for mode in TRACKED_MODES:
        short = mode.replace("petclinic-", "")

        if mode not in current:
            table_rows.append((short, "N/A", "—", "— (missing)", "—", "missing"))
            continue

        cur_em = current[mode]["emissions"]
        cur_en = current[mode]["energy_consumed"]

        if mode not in baseline:
            table_rows.append((short, f"{cur_em:.7f}", f"{cur_en:.7f}", "no baseline", "—", "new"))
            continue

        entry = baseline[mode]
        b_mean = _baseline_mean(entry)
        b_std  = _baseline_std(entry)
        b_n    = _baseline_n(entry)

        baseline_cell = f"{b_mean:.7f} ± {b_std:.7f} (n={b_n})"

        status, delta = classify(cur_em, b_mean, warn_pct, fail_pct)
        sign = "+" if delta >= 0 else ""
        table_rows.append((
            short,
            f"{cur_em:.7f}",
            f"{cur_en:.7f}",
            baseline_cell,
            f"{sign}{delta * 100:.1f}%",
            status,
        ))

        if status == "fail":
            overall = "fail"
        elif status == "warn" and overall == "pass":
            overall = "warn"

    # ---- Render markdown -----------------------------------------------------
    lines = [
        "## ⚡ Energy Regression Report\n",
        "| Mode | Emissions (kg CO₂eq) | Energy (kWh) | Baseline mean ± σ (kg CO₂eq) | Δ vs mean | Status |",
        "|---|---|---|---|---|---|",
    ]
    for short, em, en, b_cell, delta, status in table_rows:
        icon = status_icon(status)
        lines.append(f"| `{short}` | {em} | {en} | {b_cell} | {delta} | {icon} {status.upper()} |")

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
        help="Directory containing per-mode emissions CSVs (default: ./codecarbon-results)",
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
        help="Also write the markdown report to this file (for PR comments)",
    )

    args = parser.parse_args()

    if args.update_baseline:
        sys.exit(update_baseline(Path(args.results_dir), args.runs))
    else:
        sys.exit(run_comparison(args))


if __name__ == "__main__":
    main()
