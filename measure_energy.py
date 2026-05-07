
#!/usr/bin/env python3
"""
Measure energy consumption of Spring PetClinic REST using CodeCarbon.

Modes:
  startup    -- measures energy from process launch until the app is ready
  load       -- starts the app, then measures energy while sending HTTP load
  tests      -- measures energy during Maven test suite execution
  endpoints  -- starts the app, then measures energy per individual endpoint

Usage examples:
  python measure_energy.py startup
  python measure_energy.py startup --profile h2,spring-data-jpa
  python measure_energy.py load --duration 120 --concurrency 8
  python measure_energy.py tests --test-filter OwnerRestControllerTests
  python measure_energy.py endpoints --requests 200 --warmup 20
"""

import argparse
import csv
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from codecarbon import EmissionsTracker

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "http://localhost:9966/petclinic"
HEALTH_URL = f"{BASE_URL}/actuator/health"
APP_READY_TIMEOUT = 360  # seconds

LOAD_ENDPOINTS = [
    "/api/owners",
    "/api/vets",
    "/api/pettypes",
    "/api/specialties",
]

# Endpoints measured individually in 'endpoints' mode.
# IDs match the H2 seed data in src/main/resources/db/h2/data.sql.
ENDPOINT_CATALOG = [
    {"name": "list-owners",      "path": "/api/owners"},
    {"name": "list-vets",        "path": "/api/vets"},
    {"name": "list-pettypes",    "path": "/api/pettypes"},
    {"name": "list-specialties", "path": "/api/specialties"},
    {"name": "list-pets",        "path": "/api/pets"},
    {"name": "list-visits",      "path": "/api/visits"},
    {"name": "get-owner",        "path": "/api/owners/1"},
    {"name": "get-vet",          "path": "/api/vets/1"},
    {"name": "get-pettype",      "path": "/api/pettypes/1"},
    {"name": "get-specialty",    "path": "/api/specialties/1"},
    {"name": "get-pet",          "path": "/api/pets/1"},
    {"name": "get-visit",        "path": "/api/visits/1"},
    {"name": "search-owners",    "path": "/api/owners?lastName=Davis"},
    {"name": "get-owner-pet",    "path": "/api/owners/6/pets/7"},
]

PROJECT_ROOT = Path(__file__).parent

# Fixed-schema history CSVs written by this script.
# Using our own files avoids CodeCarbon's schema-drift backups resetting data.
_HISTORY_FIELDS = ["timestamp", "project_name", "emissions", "energy_consumed", "duration"]
_ENDPOINT_HISTORY_FIELDS = [
    "timestamp", "endpoint_name", "path",
    "requests", "emissions", "energy_consumed", "duration", "energy_per_request",
]


# ---------------------------------------------------------------------------
# History writers
# ---------------------------------------------------------------------------


def append_to_history(tracker: EmissionsTracker, output_dir: str) -> None:
    """Append one row to {mode}_history.csv from tracker.final_emissions_data."""
    d = tracker.final_emissions_data
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "project_name": tracker._project_name,
        "emissions": d.emissions,
        "energy_consumed": d.energy_consumed,
        "duration": d.duration,
    }
    mode = tracker._project_name.replace("petclinic-", "")
    history_path = Path(output_dir) / f"{mode}_history.csv"
    write_header = not history_path.exists()
    with open(history_path, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_HISTORY_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def append_endpoint_to_history(
    tracker: EmissionsTracker,
    endpoint: dict,
    n_requests: int,
    output_dir: str,
) -> None:
    """Append one row to endpoints_history.csv for the given endpoint."""
    d = tracker.final_emissions_data
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "endpoint_name": endpoint["name"],
        "path": endpoint["path"],
        "requests": n_requests,
        "emissions": d.emissions,
        "energy_consumed": d.energy_consumed,
        "duration": d.duration,
        "energy_per_request": d.energy_consumed / n_requests if n_requests else 0,
    }
    history_path = Path(output_dir) / "endpoints_history.csv"
    write_header = not history_path.exists()
    with open(history_path, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_ENDPOINT_HISTORY_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


# ---------------------------------------------------------------------------
# App lifecycle helpers
# ---------------------------------------------------------------------------


def start_app(profile: str, log_path: Path) -> subprocess.Popen:
    """Launch the Spring Boot app via Maven wrapper in a new process group.

    App stdout+stderr is redirected to log_path so it doesn't mix with
    script output and doesn't block on a full PIPE buffer.
    """
    cmd = ["./mvnw", "spring-boot:run", f"-Dspring-boot.run.profiles={profile}"]
    print(f"[measure_energy] Starting app: {' '.join(cmd)}", flush=True)
    print(f"[measure_energy] App log: {log_path}", flush=True)
    log_file = open(log_path, "w")
    return subprocess.Popen(
        cmd,
        cwd=PROJECT_ROOT,
        env=os.environ.copy(),
        stdout=log_file,
        stderr=log_file,
        preexec_fn=os.setsid,  # new process group so we can kill children too
    )


def wait_for_app(proc: subprocess.Popen, timeout: int = APP_READY_TIMEOUT) -> bool:
    """Poll the actuator health endpoint until the app responds OK.

    Returns False immediately if the process exits before becoming healthy.
    """
    print(f"[measure_energy] Waiting for app at {HEALTH_URL} (timeout={timeout}s)...", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            print(
                f"[measure_energy] ERROR: app process exited with code {proc.returncode}.",
                file=sys.stderr, flush=True,
            )
            return False
        try:
            resp = requests.get(HEALTH_URL, timeout=3)
            if resp.status_code == 200:
                print("[measure_energy] App is ready.", flush=True)
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(2)
    print("[measure_energy] ERROR: app did not become ready in time.", file=sys.stderr, flush=True)
    return False


def stop_app(proc: subprocess.Popen) -> None:
    """Terminate the entire process group spawned by start_app."""
    print("[measure_energy] Stopping app...", flush=True)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


# ---------------------------------------------------------------------------
# Load generator (load mode)
# ---------------------------------------------------------------------------


def run_load(duration: int, concurrency: int) -> None:
    """Hit LOAD_ENDPOINTS repeatedly with `concurrency` threads for `duration` seconds."""
    stop_event = threading.Event()
    stats = {"ok": 0, "err": 0}
    lock = threading.Lock()

    def worker() -> None:
        session = requests.Session()
        while not stop_event.is_set():
            for endpoint in LOAD_ENDPOINTS:
                if stop_event.is_set():
                    break
                try:
                    session.get(f"{BASE_URL}{endpoint}", timeout=10)
                    with lock:
                        stats["ok"] += 1
                except Exception:
                    with lock:
                        stats["err"] += 1

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    for t in threads:
        t.start()

    print(f"[measure_energy] Running load for {duration}s with {concurrency} threads...", flush=True)
    time.sleep(duration)
    stop_event.set()
    for t in threads:
        t.join()

    print(f"[measure_energy] Load done: {stats['ok']} ok, {stats['err']} errors", flush=True)


# ---------------------------------------------------------------------------
# Measurement modes
# ---------------------------------------------------------------------------


def measure_startup(args: argparse.Namespace) -> None:
    """Measure energy from process launch until the app reports healthy."""
    log_path = Path(args.output_dir) / "app-startup.log"
    tracker = EmissionsTracker(
        project_name="petclinic-startup",
        output_dir=args.output_dir,
        output_file="startup_emissions.csv",
        log_level="warning",
    )
    tracker.start()
    proc = start_app(args.profile, log_path)
    try:
        ready = wait_for_app(proc)
    finally:
        emissions = tracker.stop()
        append_to_history(tracker, args.output_dir)
        stop_app(proc)

    if not ready:
        print(f"[measure_energy] Check app log for details: {log_path}", flush=True)
        sys.exit(1)

    print(f"\n[startup] CO2eq emissions : {emissions:.6f} kg", flush=True)


def measure_load(args: argparse.Namespace) -> None:
    """Start the app, then measure energy while driving HTTP load against it."""
    log_path = Path(args.output_dir) / "app-load.log"
    proc = start_app(args.profile, log_path)
    try:
        if not wait_for_app(proc):
            print(f"[measure_energy] Check app log for details: {log_path}", flush=True)
            sys.exit(1)

        tracker = EmissionsTracker(
            project_name="petclinic-load",
            output_dir=args.output_dir,
            output_file="load_emissions.csv",
            log_level="warning",
        )
        tracker.start()
        run_load(args.duration, args.concurrency)
        emissions = tracker.stop()
        append_to_history(tracker, args.output_dir)
        print(f"\n[load] CO2eq emissions : {emissions:.6f} kg", flush=True)
    finally:
        stop_app(proc)


def measure_tests(args: argparse.Namespace) -> None:
    """Measure energy consumed by the Maven test suite."""
    cmd = ["./mvnw", "test"]
    if args.test_filter:
        cmd += [f"-Dtest={args.test_filter}"]

    print(f"[measure_energy] Running: {' '.join(cmd)}", flush=True)
    tracker = EmissionsTracker(
        project_name="petclinic-tests",
        output_dir=args.output_dir,
        output_file="tests_emissions.csv",
        log_level="warning",
    )
    tracker.start()
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    emissions = tracker.stop()
    append_to_history(tracker, args.output_dir)

    print(f"\n[tests] CO2eq emissions : {emissions:.6f} kg", flush=True)
    sys.exit(result.returncode)


def measure_endpoints(args: argparse.Namespace) -> None:
    """Start the app, then measure energy for each endpoint individually.

    For each endpoint:
      1. Warmup requests (to heat JVM JIT and DB caches — not measured)
      2. Measured requests with a dedicated CodeCarbon tracker
      3. Result appended to endpoints_history.csv

    measure_power_secs=1 is used so CodeCarbon samples frequently enough
    for short per-endpoint measurement windows.
    """
    log_path = Path(args.output_dir) / "app-endpoints.log"
    proc = start_app(args.profile, log_path)
    try:
        if not wait_for_app(proc):
            print(f"[measure_energy] Check app log for details: {log_path}", flush=True)
            sys.exit(1)

        session = requests.Session()

        # Global warmup — one pass over all endpoints to warm JVM JIT and H2 caches
        print(f"[measure_energy] Global warmup ({args.warmup} rounds over all endpoints)...", flush=True)
        for _ in range(args.warmup):
            for ep in ENDPOINT_CATALOG:
                session.get(f"{BASE_URL}{ep['path']}", timeout=10)

        # Per-endpoint measurement
        for ep in ENDPOINT_CATALOG:
            url = f"{BASE_URL}{ep['path']}"
            print(
                f"[measure_energy] {ep['name']:20s}  {url}  "
                f"({args.requests} requests) ...",
                flush=True,
            )

            tracker = EmissionsTracker(
                project_name=f"petclinic-endpoint-{ep['name']}",
                output_dir=args.output_dir,
                output_file=f"endpoint_{ep['name']}_emissions.csv",
                measure_power_secs=1,   # sample every second for short windows
                log_level="error",
            )
            tracker.start()
            for _ in range(args.requests):
                session.get(url, timeout=10)
            tracker.stop()

            append_endpoint_to_history(tracker, ep, args.requests, args.output_dir)

            epr = tracker.final_emissions_data.energy_consumed / args.requests
            print(f"  → energy/request: {epr:.3e} kWh", flush=True)

    finally:
        stop_app(proc)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure Spring PetClinic REST energy consumption via CodeCarbon.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "mode",
        choices=["startup", "load", "tests", "endpoints"],
        help="Measurement mode",
    )
    parser.add_argument(
        "--profile",
        default="postgres,spring-data-jpa",
        help="Spring active profile(s) for modes that start the app "
             "(default: postgres,spring-data-jpa)",
    )
    parser.add_argument(
        "--output-dir",
        default="./codecarbon-results",
        help="Directory for output CSVs (default: ./codecarbon-results)",
    )

    load_group = parser.add_argument_group("load mode options")
    load_group.add_argument(
        "--duration",
        type=int,
        default=60,
        help="Seconds to run HTTP load (default: 60)",
    )
    load_group.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Number of concurrent request threads (default: 4)",
    )

    test_group = parser.add_argument_group("tests mode options")
    test_group.add_argument(
        "--test-filter",
        default=None,
        metavar="PATTERN",
        help="Maven -Dtest filter, e.g. 'OwnerRestControllerTests'",
    )

    ep_group = parser.add_argument_group("endpoints mode options")
    ep_group.add_argument(
        "--requests",
        type=int,
        default=200,
        help="Requests per endpoint during measurement (default: 200)",
    )
    ep_group.add_argument(
        "--warmup",
        type=int,
        default=10,
        help="Warmup rounds over all endpoints before measuring (default: 10)",
    )

    args = parser.parse_args()
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    dispatch = {
        "startup": measure_startup,
        "load": measure_load,
        "tests": measure_tests,
        "endpoints": measure_endpoints,
    }
    dispatch[args.mode](args)


if __name__ == "__main__":
    main()
