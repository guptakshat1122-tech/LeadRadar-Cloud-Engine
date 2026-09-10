import os
import sys
import time
import subprocess
import socket
import shutil
import re
from datetime import datetime

# ============================================================
# BAWA MASTER CONTROLLER v2.2
# Compatible with:
#   1) domain_sniper.py v2.2
#   2) 1_domain_filter.py v3.1
#   3) 2_deep_xray_scanner.py v3.1
#   4) 3_lead_categorizer.py v3.1
#
# Design:
#   - One active date at a time
#   - Date lock controls resume
#   - No outer retry storm for X-Ray
#   - X-Ray owns HTTP retries + SUCCESS/DEAD cache semantics
#   - Categorizer owns partial-output resume + Groq quota handling
#   - Non-zero categorizer exit never deletes partial work
#   - Final CSV is archived only after a real final output exists
#   - All paths are repo-relative / GitHub Actions safe
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DATA_DIR = os.path.join(BASE_DIR, "daily_domains")
STATE_DIR = os.path.join(BASE_DIR, "state")
MASTER_DIR = os.path.join(BASE_DIR, "master_control_room")

SNIPER_SCRIPT = os.path.join(BASE_DIR, "domain_sniper.py")
FILTER_1_SCRIPT = os.path.join(BASE_DIR, "1_domain_filter.py")
FILTER_2_SCRIPT = os.path.join(BASE_DIR, "2_deep_xray_scanner.py")
FILTER_3_SCRIPT = os.path.join(BASE_DIR, "3_lead_categorizer.py")

PYTHON = sys.executable

# GitHub Actions gives the job its own hard ceiling.
# Keep a safety margin below the runner's maximum.
MAX_RUNTIME_SECONDS = int(
    os.environ.get("MAX_RUNTIME_SECONDS", 5 * 3600 + 30 * 60)
)

# Per-stage safety timeouts.
SNIPER_TIMEOUT = int(os.environ.get("SNIPER_TIMEOUT", 30 * 60))
FILTER_1_TIMEOUT = int(os.environ.get("FILTER_1_TIMEOUT", 10 * 60))
FILTER_2_TIMEOUT = int(os.environ.get("FILTER_2_TIMEOUT", 2 * 60 * 60))
FILTER_3_TIMEOUT = int(os.environ.get("FILTER_3_TIMEOUT", 4 * 60 * 60))

DATE_LOCK_FILE = "date_lock.txt"
RAW_INPUT_FILE = "domain-names.txt"
FILTER_1_OUTPUT = "premium_domains.txt"
FILTER_1_REPORT = "premium_domain_report.txt"
XRAY_DONE_FILE = "filter_2.done"
XRAY_CACHE_FILE = "scanned_cache.txt"
XRAY_OUTPUT_FILE = "Ultimate_God_Leads.csv"

CAT_OUTPUT_FILE = "Bawa_Categorized_Leads.csv"
CAT_PARTIAL_FILE = "Bawa_Categorized_Leads.partial.csv"

for directory in (RAW_DATA_DIR, STATE_DIR, MASTER_DIR):
    os.makedirs(directory, exist_ok=True)


# ============================================================
# LOGGING / BASIC HELPERS
# ============================================================

def log(message):
    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}",
        flush=True,
    )


def check_internet():
    """Quick connectivity test; stage scripts still perform their own retries."""
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3)
        return True
    except OSError:
        return False


def elapsed_seconds(run_start):
    return time.time() - run_start


def budget_remaining(run_start, reserve_seconds=30):
    return MAX_RUNTIME_SECONDS - elapsed_seconds(run_start) - reserve_seconds


def within_budget(run_start, required_seconds=0):
    return budget_remaining(run_start, reserve_seconds=30) > required_seconds


def require_stage_budget(run_start, stage_name, timeout_seconds):
    """
    Refuse to launch a stage unless its full configured timeout plus the
    controller safety reserve still fits inside this run's remaining budget.
    This prevents the runner from killing a long stage before the controller
    gets a chance to preserve state and exit cleanly.
    """
    if run_start is None:
        return True

    remaining = budget_remaining(run_start, reserve_seconds=30)
    if remaining <= timeout_seconds:
        log(
            f"⏱️ Not enough budget to launch {stage_name}: "
            f"remaining≈{max(0, int(remaining))}s, "
            f"required={timeout_seconds}s + 30s safety reserve. "
            "Stage not launched; state remains resumable."
        )
        return False

    return True


# ============================================================
# FILE / DATE DISCOVERY
# ============================================================

def state_path(filename):
    return os.path.join(STATE_DIR, filename)


def master_path(filename):
    return os.path.join(MASTER_DIR, filename)


def find_raw_file(target_date_str):
    """
    Find WhoisDS raw TXT for one exact target date.
    Prefer an exact date-containing filename.
    """
    if not os.path.isdir(RAW_DATA_DIR):
        return None

    candidates = []
    for filename in os.listdir(RAW_DATA_DIR):
        if not filename.lower().endswith(".txt"):
            continue
        if target_date_str not in filename:
            continue

        full_path = os.path.join(RAW_DATA_DIR, filename)
        if os.path.isfile(full_path):
            candidates.append(full_path)

    if not candidates:
        return None

    # Deterministic selection if more than one candidate exists.
    candidates.sort()
    return candidates[0]


def extract_date_from_filename(filename):
    match = re.search(r"\d{4}-\d{2}-\d{2}", filename)
    return match.group(0) if match else None


def get_all_raw_data_dates():
    dates_found = []

    if os.path.isdir(RAW_DATA_DIR):
        for filename in os.listdir(RAW_DATA_DIR):
            if not filename.lower().endswith(".txt"):
                continue

            date_str = extract_date_from_filename(filename)
            if not date_str:
                continue

            try:
                dates_found.append(
                    datetime.strptime(date_str, "%Y-%m-%d").date()
                )
            except ValueError:
                pass

    return sorted(set(dates_found))


# ============================================================
# WORKSPACE / LOCK STATE
# ============================================================

def read_locked_date():
    path = state_path(DATE_LOCK_FILE)

    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = handle.read().strip()
        return value or None
    except OSError as exc:
        log(f"⚠️ Could not read date lock: {exc}")
        return None


def write_locked_date(target_date_str):
    tmp_path = state_path(f"{DATE_LOCK_FILE}.tmp")
    final_path = state_path(DATE_LOCK_FILE)

    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(target_date_str)

    os.replace(tmp_path, final_path)


def remove_file(filename):
    path = state_path(filename)
    try:
        if os.path.exists(path):
            os.remove(path)
            log(f"   Removed: {filename}")
    except OSError as exc:
        log(f"⚠️ Could not remove {filename}: {exc}")


def is_workspace_dirty():
    """
    A lock itself means there is an active/resumable date.
    Stage artifacts also count as dirty state.
    """
    tracked = [
        DATE_LOCK_FILE,
        RAW_INPUT_FILE,
        FILTER_1_OUTPUT,
        FILTER_1_REPORT,
        XRAY_DONE_FILE,
        XRAY_CACHE_FILE,
        XRAY_OUTPUT_FILE,
        CAT_OUTPUT_FILE,
        CAT_PARTIAL_FILE,
    ]

    return any(os.path.exists(state_path(name)) for name in tracked)


def cleanup_success_workspace():
    """
    Remove only pipeline-owned state artifacts after final archive succeeds.
    """
    for filename in [
        RAW_INPUT_FILE,
        FILTER_1_OUTPUT,
        FILTER_1_REPORT,
        XRAY_DONE_FILE,
        XRAY_CACHE_FILE,
        XRAY_OUTPUT_FILE,
        CAT_OUTPUT_FILE,
        CAT_PARTIAL_FILE,
        DATE_LOCK_FILE,
    ]:
        remove_file(filename)


# ============================================================
# STAGE STATUS
# ============================================================

def get_step_status():
    return {
        "raw_file": os.path.exists(state_path(RAW_INPUT_FILE)),
        "step1_done": os.path.exists(state_path(FILTER_1_OUTPUT)),
        "step2_done": os.path.exists(state_path(XRAY_DONE_FILE)),
        "step3_done": os.path.exists(state_path(CAT_OUTPUT_FILE)),
        "cat_partial": os.path.exists(state_path(CAT_PARTIAL_FILE)),
        "has_cache": os.path.exists(state_path(XRAY_CACHE_FILE)),
        "has_god": os.path.exists(state_path(XRAY_OUTPUT_FILE)),
        "date_lock": read_locked_date(),
    }


def log_status():
    status = get_step_status()
    compact = (
        f"raw={status['raw_file']} | "
        f"filter1={status['step1_done']} | "
        f"xray_done={status['step2_done']} | "
        f"xray_cache={status['has_cache']} | "
        f"xray_csv={status['has_god']} | "
        f"cat_final={status['step3_done']} | "
        f"cat_partial={status['cat_partial']} | "
        f"lock={status['date_lock']}"
    )
    log(f"STATUS: {compact}")


# ============================================================
# SUBPROCESS EXECUTION
# ============================================================

def run_stage(script_path, timeout):
    """
    Run a stage from STATE_DIR so its relative input/output files
    land in the controller's state workspace.
    """
    return subprocess.run(
        [PYTHON, script_path],
        check=True,
        cwd=STATE_DIR,
        timeout=timeout,
    )


# ============================================================
# ARCHIVE VALIDATION
# ============================================================

def is_valid_final_csv(path):
    """
    Minimal sanity check. Do not declare completion from the mere
    existence of a zero-byte file.
    """
    if not os.path.isfile(path):
        return False

    try:
        if os.path.getsize(path) < 50:
            return False

        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            sample = handle.read(1000)

        if not sample.strip():
            return False

        # Preserve the old controller's protection against placeholder output.
        if "NO_DATA" in sample.upper():
            return False

        # New categorizer's expected first column.
        if "Domain" not in sample:
            return False

        return True
    except OSError:
        return False


def is_valid_archive(path):
    return is_valid_final_csv(path)


# ============================================================
# STEP 1 — SNIPER
# ============================================================

def run_sniper_for_date(target_date_str):
    """
    IMPORTANT:
    v2.2 sniper supports CLI target date. Pass it explicitly so the
    controller and sniper agree on exactly which date must be present.
    """
    log(
        "STEP 1: Firing Sniper "
        f"(historical sync + target-date verification: {target_date_str})..."
    )

    if not check_internet():
        log("⚠️ Internet unavailable before Sniper.")
        return "PAUSED"

    try:
        subprocess.run(
            [PYTHON, SNIPER_SCRIPT, target_date_str],
            check=True,
            cwd=BASE_DIR,
            timeout=SNIPER_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log("❌ Sniper timed out. Keeping date lock for next run.")
        return "PAUSED"
    except subprocess.CalledProcessError as exc:
        log(f"❌ Sniper exited with code {exc.returncode}. Keeping state.")
        return "ERROR"
    except OSError as exc:
        log(f"❌ Could not launch Sniper: {exc}")
        return "ERROR"

    downloaded_file = find_raw_file(target_date_str)
    target_input = state_path(RAW_INPUT_FILE)

    if not downloaded_file:
        log(
            f"ℹ️ Raw WhoisDS file for {target_date_str} is not available yet."
        )
        return "MISSING"

    try:
        shutil.copy2(downloaded_file, target_input)
        log(f"✅ Raw data piped for {target_date_str}: {downloaded_file}")
        return "SUCCESS"
    except OSError as exc:
        log(f"❌ Could not copy raw data into state: {exc}")
        return "ERROR"


# ============================================================
# STEP 2 — DOMAIN FILTER
# ============================================================

def run_domain_filter():
    log("STEP 2: Running Level 1 Domain Filter...")

    if not os.path.exists(state_path(RAW_INPUT_FILE)):
        log("❌ domain-names.txt missing before Filter 1.")
        return "ERROR"

    try:
        run_stage(FILTER_1_SCRIPT, FILTER_1_TIMEOUT)
    except subprocess.TimeoutExpired:
        log("❌ Filter 1 timed out. Keeping workspace for next run.")
        return "PAUSED"
    except subprocess.CalledProcessError as exc:
        log(f"❌ Filter 1 exited with code {exc.returncode}. Keeping state.")
        return "ERROR"
    except OSError as exc:
        log(f"❌ Could not launch Filter 1: {exc}")
        return "ERROR"

    output = state_path(FILTER_1_OUTPUT)

    if not os.path.exists(output):
        log("❌ Filter 1 finished but premium_domains.txt is missing.")
        return "ERROR"

    log("✅ Level 1 Domain Filter complete.")
    return "SUCCESS"


# ============================================================
# STEP 3 — X-RAY
# ============================================================

def run_xray():
    """
    v3.1 X-Ray owns:
      - per-thread HTTP sessions
      - HTTP retry logic
      - SUCCESS / DEAD cache entries
      - terminal handling of expected network/request failures
      - filter_2.done creation only when no retry work remains

    Therefore the master controller deliberately does NOT wrap this
    stage in its own 5-attempt loop. That old outer loop was the retry storm.
    """
    done_file = state_path(XRAY_DONE_FILE)

    if os.path.exists(done_file):
        log("STEP 3: X-Ray already marked done — skipping.")
        return "SUCCESS"

    log("STEP 3: Running X-Ray Scanner (Level 2)...")

    if not os.path.exists(state_path(FILTER_1_OUTPUT)):
        log("❌ premium_domains.txt missing before X-Ray.")
        return "ERROR"

    if not check_internet():
        log(
            "⚠️ Internet is currently unavailable. "
            "Let the next scheduled run resume X-Ray."
        )
        return "PAUSED"

    try:
        run_stage(FILTER_2_SCRIPT, FILTER_2_TIMEOUT)
    except subprocess.TimeoutExpired:
        log(
            "⏱️ X-Ray timed out. "
            "Its cache/output remain intact; next run will resume."
        )
        return "PAUSED"
    except subprocess.CalledProcessError as exc:
        log(
            f"❌ X-Ray exited with code {exc.returncode}. "
            "Keeping cache/output for next run."
        )
        return "ERROR"
    except OSError as exc:
        log(f"❌ Could not launch X-Ray: {exc}")
        return "ERROR"

    if os.path.exists(done_file):
        log("✅ X-Ray completed — filter_2.done present.")
        return "SUCCESS"

    log(
        "⚠️ X-Ray returned without filter_2.done. "
        "This means work is not terminally complete yet; preserving state."
    )
    return "PAUSED"


# ============================================================
# STEP 4 — GROQ CATEGORIZER
# ============================================================

def run_categorizer():
    """
    v3.1 categorizer owns:
      - final CSV
      - .partial.csv resume state
      - strict JSON validation
      - rate limiting / Retry-After handling
      - quota hard-wall detection

    Controller rule:
      - never delete .partial on timeout/failure
      - never fabricate final completion
      - non-zero exit means stop this run and let the next scheduled run resume
    """
    final_file = state_path(CAT_OUTPUT_FILE)
    partial_file = state_path(CAT_PARTIAL_FILE)

    if os.path.exists(final_file):
        log("STEP 4: Categorizer final output already exists — skipping.")
        return "SUCCESS"

    log("STEP 4: Running AI Categorizer (Groq)...")

    if not os.path.exists(state_path(XRAY_DONE_FILE)):
        log("❌ X-Ray is not marked done before Categorizer.")
        return "ERROR"

    if not os.path.exists(state_path(XRAY_OUTPUT_FILE)):
        log("❌ Ultimate_God_Leads.csv missing before Categorizer.")
        return "ERROR"

    try:
        run_stage(FILTER_3_SCRIPT, FILTER_3_TIMEOUT)
    except subprocess.TimeoutExpired:
        if os.path.exists(partial_file):
            log(
                "⏱️ Categorizer timed out. "
                ".partial.csv preserved for next run."
            )
        else:
            log(
                "⏱️ Categorizer timed out before partial output appeared. "
                "State preserved for next run."
            )
        return "PAUSED"
    except subprocess.CalledProcessError as exc:
        if os.path.exists(partial_file):
            log(
                f"⚠️ Categorizer exited {exc.returncode}; "
                ".partial.csv preserved for next run."
            )
        else:
            log(
                f"⚠️ Categorizer exited {exc.returncode}; "
                "no partial file found. Will retry on next run."
            )
        return "PAUSED"
    except OSError as exc:
        log(f"❌ Could not launch Categorizer: {exc}")
        return "ERROR"

    if os.path.exists(final_file):
        log("✅ Categorizer produced final output.")
        return "SUCCESS"

    if os.path.exists(partial_file):
        log(
            "ℹ️ Categorizer left partial progress intentionally. "
            "No final file yet; next run will resume."
        )
        return "PAUSED"

    log(
        "⚠️ Categorizer returned without final or partial output. "
        "Preserving lock and stopping this run."
    )
    return "PAUSED"


# ============================================================
# FINAL ARCHIVE
# ============================================================

def archive_final_output(target_date_str):
    final_temp = state_path(CAT_OUTPUT_FILE)
    archived_out = master_path(
        f"Final_Extracted_Leads_{target_date_str}.csv"
    )

    if not is_valid_final_csv(final_temp):
        log("❌ Final categorized output is missing or invalid.")
        return "ERROR"

    try:
        # Replace only after the new final output has passed sanity checks.
        os.replace(final_temp, archived_out)
    except OSError as exc:
        log(f"❌ Could not archive final CSV: {exc}")
        return "ERROR"

    if not is_valid_archive(archived_out):
        log("❌ Archived CSV failed post-move validation.")
        return "ERROR"

    log(f"🎉 DONE! VIP List archived: {archived_out}")
    cleanup_success_workspace()
    log(f"Workspace clean. {target_date_str} closed ✅")
    return "SUCCESS"


# ============================================================
# PIPELINE FOR ONE DATE
# ============================================================

def fire_the_pipeline(target_date_str, is_resume=False, run_start=None):
    log("")
    log("============================================================")
    log(f"[!!!] STARTING PIPELINE FOR DATE: {target_date_str} [!!!]")
    log(f"Resume mode: {is_resume}")
    log("============================================================")

    # Always refresh the lock at the beginning of an active date.
    write_locked_date(target_date_str)

    if run_start is not None and not within_budget(run_start):
        log("⏱️ Not enough run budget left to start another stage.")
        return "PAUSED"

    # ---------------- STEP 1: SNIPER ----------------
    # On resume, existing state is authoritative. Do not re-run Sniper
    # against a partially processed date.
    if not is_resume:
        if not os.path.exists(state_path(RAW_INPUT_FILE)):
            if not require_stage_budget(run_start, "Sniper", SNIPER_TIMEOUT):
                return "PAUSED"
            sniper_status = run_sniper_for_date(target_date_str)

            if sniper_status == "MISSING":
                log(
                    f"ℹ️ WhoisDS has not exposed usable data for "
                    f"{target_date_str} yet."
                )
                remove_file(DATE_LOCK_FILE)
                return "MISSING"

            if sniper_status != "SUCCESS":
                return sniper_status
        else:
            log(
                "STEP 1: Existing domain-names.txt found — "
                "using existing state instead of re-running Sniper."
            )
    else:
        if os.path.exists(state_path(RAW_INPUT_FILE)):
            log("STEP 1: Resume — existing raw input preserved.")
        else:
            # A resume without raw input is not safely resumable.
            log(
                "⚠️ Resume requested but domain-names.txt is missing. "
                "Attempting target-date Sniper recovery..."
            )
            if not require_stage_budget(run_start, "Sniper", SNIPER_TIMEOUT):
                return "PAUSED"
            sniper_status = run_sniper_for_date(target_date_str)

            if sniper_status != "SUCCESS":
                return sniper_status

    # ---------------- STEP 2: FILTER 1 ----------------
    if not os.path.exists(state_path(FILTER_1_OUTPUT)):
        if not require_stage_budget(run_start, "Filter 1", FILTER_1_TIMEOUT):
            return "PAUSED"
        status = run_domain_filter()
        if status != "SUCCESS":
            return status
    else:
        log("STEP 2: Already done — skipping.")

    # ---------------- STEP 3: X-RAY ----------------
    if not os.path.exists(state_path(XRAY_DONE_FILE)):
        if not require_stage_budget(run_start, "X-Ray", FILTER_2_TIMEOUT):
            return "PAUSED"
        status = run_xray()
        if status != "SUCCESS":
            return status
    else:
        log("STEP 3: Already done — skipping.")

    # ---------------- STEP 4: CATEGORIZER ----------------
    if not os.path.exists(state_path(CAT_OUTPUT_FILE)):
        if not require_stage_budget(run_start, "AI Categorizer", FILTER_3_TIMEOUT):
            return "PAUSED"
        status = run_categorizer()
        if status != "SUCCESS":
            return status
    else:
        log("STEP 4: Already done — skipping.")

    # ---------------- STEP 5: ARCHIVE ----------------
    return archive_final_output(target_date_str)


# ============================================================
# BACKLOG HELPERS
# ============================================================

def final_archive_path(target_date_str):
    return master_path(f"Final_Extracted_Leads_{target_date_str}.csv")


def needs_backlog_processing(target_date_str):
    """
    True if there is raw data but no trustworthy final archive.
    """
    archive = final_archive_path(target_date_str)

    if not os.path.exists(archive):
        return True

    return not is_valid_archive(archive)


def clean_invalid_archive(target_date_str):
    archive = final_archive_path(target_date_str)

    if os.path.exists(archive) and not is_valid_archive(archive):
        try:
            os.remove(archive)
            log(f"🧹 Removed invalid existing archive: {archive}")
        except OSError as exc:
            log(f"⚠️ Could not remove invalid archive: {exc}")


# ============================================================
# MAIN
# ============================================================

def main():
    log("============================================================")
    log("BAWA MASTER CONTROLLER v2.1 ONLINE")
    log("Single-run / bounded-budget / resume-safe mode")
    log("============================================================")

    run_start = time.time()

    while True:
        if not within_budget(run_start):
            log(
                "⏱️ Time budget khatam — exiting cleanly. "
                "Next scheduled run will continue the lock/state."
            )
            break

        if not check_internet():
            log(
                "⚠️ Internet unavailable right now — "
                "exiting this run without destroying state."
            )
            break

        # ----------------------------------------------------
        # SCENARIO 1: ACTIVE / DIRTY WORKSPACE
        # ----------------------------------------------------
        if is_workspace_dirty():
            locked_date = read_locked_date()

            if locked_date:
                log(f"🛑 RESUME: Active date lock found for {locked_date}")
                log_status()

                result = fire_the_pipeline(
                    locked_date,
                    is_resume=True,
                    run_start=run_start,
                )

                if result == "SUCCESS":
                    log(f"✅ Resumed date {locked_date} successfully.")
                    continue

                if result == "MISSING":
                    log(
                        f"ℹ️ Locked date {locked_date} is still missing upstream data."
                    )
                    remove_file(DATE_LOCK_FILE)
                    break

                # IMPORTANT:
                # Do not immediately call the same date again in this run.
                # This prevents quota/retry/error spin loops.
                log(
                    f"⏸️ Resume for {locked_date} returned {result}. "
                    "Keeping state for the next scheduled run."
                )
                break

            # Dirty state with no lock is not safely attributable to a date.
            # Remove only pipeline artifacts, then start cleanly.
            log(
                "⚠️ Dirty workspace but no date lock. "
                "Treating it as orphaned pipeline state."
            )

            for filename in [
                RAW_INPUT_FILE,
                FILTER_1_OUTPUT,
                FILTER_1_REPORT,
                XRAY_DONE_FILE,
                XRAY_CACHE_FILE,
                XRAY_OUTPUT_FILE,
                CAT_OUTPUT_FILE,
                CAT_PARTIAL_FILE,
            ]:
                remove_file(filename)

            continue

        # ----------------------------------------------------
        # SCENARIO 2: HISTORICAL BACKLOG
        # ----------------------------------------------------
        log("Scanning all raw data dates...")
        raw_dates = get_all_raw_data_dates()

        backlog_processed = False
        backlog_budget_blocked = False
        backlog_stop_run = False

        for raw_date in raw_dates:
            if not within_budget(run_start):
                log("⏱️ Not enough budget left for another backlog date.")
                backlog_budget_blocked = True
                break

            date_str = raw_date.strftime("%Y-%m-%d")

            if not needs_backlog_processing(date_str):
                continue

            clean_invalid_archive(date_str)

            log(f"🎯 Processing backlog date: {date_str}")

            result = fire_the_pipeline(
                date_str,
                is_resume=False,
                run_start=run_start,
            )

            backlog_processed = True

            if result == "SUCCESS":
                log(f"✅ Backlog date {date_str} complete.")
                # Only a successful date allows another pipeline attempt
                # in this same controller run.
                break

            if result == "MISSING":
                log(
                    f"ℹ️ Backlog date {date_str} is missing upstream raw data."
                )
                # MISSING means upstream data is not available yet. Clear the
                # active lock immediately so the next scheduled run starts
                # fresh instead of unnecessarily entering resume mode.
                remove_file(DATE_LOCK_FILE)
                backlog_stop_run = True
                break

            # IMPORTANT: PAUSED/ERROR must terminate this controller run.
            # Do not `continue` the outer while-loop here, because fire_the_pipeline
            # has already written the active date lock and the next iteration
            # would immediately re-enter Scenario 1 and launch the same date again.
            log(
                f"⏸️ Backlog date {date_str} returned {result}. "
                "State remains resumable for the next scheduled run. Exiting this run."
            )
            backlog_stop_run = True
            break

        if backlog_budget_blocked or backlog_stop_run:
            break

        if backlog_processed:
            # SUCCESS is the only result that reaches here. Re-scan state so
            # the next backlog date can be picked without an immediate retry
            # of the same failed/paused date.
            continue

        # ----------------------------------------------------
        # SCENARIO 3: TODAY
        # ----------------------------------------------------
        today_str = datetime.now().date().strftime("%Y-%m-%d")
        today_archive = final_archive_path(today_str)

        if is_valid_archive(today_archive):
            log(
                f"✅ Today's archive already exists and is valid: "
                f"{today_archive}"
            )
            break

        log(f"Fetching today's data: {today_str}...")

        today_result = fire_the_pipeline(
            today_str,
            is_resume=False,
            run_start=run_start,
        )

        if today_result == "SUCCESS":
            log(f"✅ Today's pipeline completed: {today_str}")
        elif today_result == "MISSING":
            log(
                f"ℹ️ Aaj ({today_str}) ka WhoisDS data abhi available nahi hai."
            )
            # MISSING is not a failure state. Remove lock so next cron
            # can attempt a fresh start.
            remove_file(DATE_LOCK_FILE)
        else:
            log(
                f"⏸️ Today's pipeline returned {today_result}. "
                "Keeping state for the next scheduled run."
            )

        # One today's attempt per scheduled run.
        break

    log("Master Controller run complete. Exiting.")


if __name__ == "__main__":
    main()
