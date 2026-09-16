import csv
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime

# ============================================================
# BAWA MASTER CONTROLLER v3.0 - QUEUE ARCHITECTURE
# Compatible with:
#   1) domain_sniper.py v2.2
#   2) 1_domain_filter.py v3.1
#   3) 2_deep_xray_scanner.py v3.1
#   4) 3_lead_categorizer.py v3.1
#
# CORE CHANGE:
#   Raw collection, website intelligence, and AI categorization
#   are now decoupled into queues.
#
#   WhoisDS can keep harvesting new dates even when Groq is blocked.
#   X-Ray runs per-date workspaces.
#   AI consumes a GLOBAL pending queue across all completed X-Ray dates.
#   Categorized rows are retained in one canonical partial registry.
#   A date is archived only after every X-Ray lead for that date has
#   a successful AI classification.
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DATA_DIR = os.path.join(BASE_DIR, "daily_domains")
PROCESSING_QUEUE_DIR = os.path.join(BASE_DIR, "processing_queue")
AI_QUEUE_DIR = os.path.join(BASE_DIR, "ai_queue")
AI_ENGINE_DIR = os.path.join(AI_QUEUE_DIR, "engine")
MASTER_DIR = os.path.join(BASE_DIR, "master_control_room")
STATE_DIR = os.path.join(BASE_DIR, "state")

SNIPER_SCRIPT = os.path.join(BASE_DIR, "domain_sniper.py")
FILTER_1_SCRIPT = os.path.join(BASE_DIR, "1_domain_filter.py")
FILTER_2_SCRIPT = os.path.join(BASE_DIR, "2_deep_xray_scanner.py")
FILTER_3_SCRIPT = os.path.join(BASE_DIR, "3_lead_categorizer.py")

PYTHON = sys.executable

MAX_RUNTIME_SECONDS = int(
    os.environ.get("MAX_RUNTIME_SECONDS", 5 * 3600 + 30 * 60)
)
SAFETY_RESERVE_SECONDS = int(os.environ.get("SAFETY_RESERVE_SECONDS", 60))
MIN_STAGE_SECONDS = int(os.environ.get("MIN_STAGE_SECONDS", 120))

SNIPER_TIMEOUT = int(os.environ.get("SNIPER_TIMEOUT", 30 * 60))
FILTER_1_TIMEOUT = int(os.environ.get("FILTER_1_TIMEOUT", 10 * 60))
FILTER_2_TIMEOUT = int(os.environ.get("FILTER_2_TIMEOUT", 2 * 60 * 60))
FILTER_3_TIMEOUT = int(os.environ.get("FILTER_3_TIMEOUT", 4 * 60 * 60))

# Persistent state inside the queue architecture.
ACTIVE_DATE_FILE = os.path.join(PROCESSING_QUEUE_DIR, "active_date.txt")
AI_CANONICAL_PARTIAL = os.path.join(
    AI_QUEUE_DIR, "Bawa_Categorized_Leads.partial.csv"
)
AI_ENGINE_INPUT = os.path.join(AI_ENGINE_DIR, "Ultimate_God_Leads.csv")
AI_ENGINE_FINAL = os.path.join(AI_ENGINE_DIR, "Bawa_Categorized_Leads.csv")
AI_ENGINE_PARTIAL = os.path.join(
    AI_ENGINE_DIR, "Bawa_Categorized_Leads.partial.csv"
)
AI_QUEUE_BUILD_TMP = os.path.join(AI_QUEUE_DIR, ".pending_build.csv")

RAW_FILE_RE = re.compile(r"^(?:Whois_Leads_Extracted_|.*?)(\d{4}-\d{2}-\d{2}).*\.txt$", re.I)

for directory in (
    RAW_DATA_DIR,
    PROCESSING_QUEUE_DIR,
    AI_QUEUE_DIR,
    AI_ENGINE_DIR,
    MASTER_DIR,
    STATE_DIR,
):
    os.makedirs(directory, exist_ok=True)


def log(message):
    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}",
        flush=True,
    )


def check_internet():
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3)
        return True
    except OSError:
        return False


def elapsed_seconds(run_start):
    return time.time() - run_start


def remaining_budget(run_start):
    return MAX_RUNTIME_SECONDS - elapsed_seconds(run_start) - SAFETY_RESERVE_SECONDS


def stage_timeout(run_start, configured_timeout):
    """Return a safe dynamic timeout that never exceeds remaining controller budget."""
    return max(0, min(configured_timeout, int(remaining_budget(run_start))))


def can_start_stage(run_start, stage_name, minimum_seconds=MIN_STAGE_SECONDS):
    remaining = remaining_budget(run_start)
    if remaining < minimum_seconds:
        log(
            f"⏱️ Not enough controller budget for {stage_name}: "
            f"remaining≈{max(0, int(remaining))}s, minimum={minimum_seconds}s."
        )
        return False
    return True


def run_stage(script_path, cwd, timeout):
    return subprocess.run(
        [PYTHON, script_path],
        check=True,
        cwd=cwd,
        timeout=timeout,
    )


def atomic_write_text(path, text):
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def safe_remove(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        log(f"⚠️ Could not remove {path}: {exc}")


def read_active_date():
    try:
        if os.path.exists(ACTIVE_DATE_FILE):
            with open(ACTIVE_DATE_FILE, "r", encoding="utf-8") as handle:
                value = handle.read().strip()
                return value or None
    except OSError as exc:
        log(f"⚠️ Active date read failed: {exc}")
    return None


def write_active_date(date_str):
    atomic_write_text(ACTIVE_DATE_FILE, date_str.strip() + "\n")


def clear_active_date():
    safe_remove(ACTIVE_DATE_FILE)


def raw_file_for_date(date_str):
    exact = []
    try:
        for filename in os.listdir(RAW_DATA_DIR):
            if not filename.lower().endswith(".txt"):
                continue
            if date_str in filename:
                exact.append(os.path.join(RAW_DATA_DIR, filename))
    except OSError as exc:
        log(f"⚠️ Raw-data listing failed: {exc}")
        return None

    if not exact:
        return None
    exact.sort()
    return exact[0]


def all_raw_dates():
    dates = set()
    try:
        filenames = os.listdir(RAW_DATA_DIR)
    except OSError as exc:
        log(f"⚠️ Could not scan raw data directory: {exc}")
        return []

    for filename in filenames:
        if not filename.lower().endswith(".txt"):
            continue
        match = re.search(r"\d{4}-\d{2}-\d{2}", filename)
        if match:
            try:
                datetime.strptime(match.group(0), "%Y-%m-%d")
                dates.add(match.group(0))
            except ValueError:
                pass
    return sorted(dates)


def workspace_for_date(date_str):
    return os.path.join(PROCESSING_QUEUE_DIR, date_str)


def source_xray_file(date_str):
    return os.path.join(workspace_for_date(date_str), "Ultimate_God_Leads.csv")


def source_xray_done(date_str):
    return os.path.join(workspace_for_date(date_str), "filter_2.done")


def final_archive_for_date(date_str):
    return os.path.join(
        MASTER_DIR, f"Final_Extracted_Leads_{date_str}.csv"
    )


def valid_csv(path):
    if not os.path.exists(path) or os.path.getsize(path) < 20:
        return False
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            return bool(reader.fieldnames)
    except (OSError, csv.Error):
        return False


def load_rows(path):
    if not os.path.exists(path):
        return [], []
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        return rows, list(reader.fieldnames or [])


def migrate_legacy_state():
    """Migrate the old single-date state/ layout into the new queue layout once.

    This is deliberately conservative: files are copied into their new queue
    location first, validated, then removed from legacy state. Existing queue
    files always win so a repeated migration cannot overwrite newer progress.
    """
    legacy_lock = os.path.join(STATE_DIR, "date_lock.txt")
    if not os.path.exists(legacy_lock):
        return

    try:
        with open(legacy_lock, "r", encoding="utf-8") as handle:
            date_str = handle.read().strip()
    except OSError as exc:
        log(f"⚠️ Could not read legacy date lock: {exc}")
        return

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        log("⚠️ Legacy date lock is invalid; leaving legacy state untouched.")
        return

    log(f"🔄 LEGACY MIGRATION: importing old state for {date_str} into queue architecture...")
    workspace = workspace_for_date(date_str)
    os.makedirs(workspace, exist_ok=True)

    files_to_copy = (
        "domain-names.txt",
        "premium_domains.txt",
        "premium_domain_report.txt",
        "Ultimate_God_Leads.csv",
        "scanned_cache.txt",
        "filter_2.done",
    )

    for name in files_to_copy:
        src = os.path.join(STATE_DIR, name)
        dst = os.path.join(workspace, name)
        if not os.path.exists(src):
            continue
        if not os.path.exists(dst):
            try:
                shutil.copy2(src, dst)
                log(f"   ✅ Migrated {name}")
            except OSError as exc:
                log(f"   ⚠️ Could not migrate {name}: {exc}")

    # Migrate the legacy AI progress into the canonical registry, adding
    # source-date metadata that did not exist in the old architecture.
    legacy_partial = os.path.join(STATE_DIR, "Bawa_Categorized_Leads.partial.csv")
    legacy_final = os.path.join(STATE_DIR, "Bawa_Categorized_Leads.csv")
    legacy_sources = [path for path in (legacy_partial, legacy_final) if valid_csv(path)]

    if legacy_sources:
        merged_by_domain = {}
        merged_fields = []
        for src in legacy_sources:
            try:
                rows, fields = load_rows(src)
            except Exception as exc:
                log(f"   ⚠️ Could not read legacy AI output {os.path.basename(src)}: {exc}")
                continue

            for field in fields:
                if field not in merged_fields:
                    merged_fields.append(field)
            for row in rows:
                domain = normalize_domain(row.get("Domain", ""))
                if not domain:
                    continue
                row["Domain"] = domain
                row["_Source_Date"] = date_str
                merged_by_domain[domain] = row

        if merged_by_domain:
            if "_Source_Date" not in merged_fields:
                merged_fields.append("_Source_Date")
            temp = AI_CANONICAL_PARTIAL + ".tmp"
            os.makedirs(AI_QUEUE_DIR, exist_ok=True)
            with open(temp, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=merged_fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(merged_by_domain.values())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, AI_CANONICAL_PARTIAL)
            log(
                f"   ✅ Migrated legacy AI progress: "
                f"{len(merged_by_domain):,} classified leads."
            )

    # If the old final categorizer output existed, it is now represented in
    # the canonical registry and should not remain in the legacy engine path.
    for legacy_path in (
        legacy_partial,
        legacy_final,
        os.path.join(STATE_DIR, "date_lock.txt"),
    ):
        safe_remove(legacy_path)

    log(f"✅ Legacy migration complete for {date_str}.")


def normalize_domain(value):
    value = (value or "").strip().lower()
    value = re.sub(r"^https?://", "", value)
    value = value.split("/", 1)[0]
    return value.rstrip(".")


def load_canonical_domains():
    domains = set()
    if not os.path.exists(AI_CANONICAL_PARTIAL):
        return domains
    try:
        with open(
            AI_CANONICAL_PARTIAL,
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                domain = normalize_domain(row.get("Domain", ""))
                if domain:
                    domains.add(domain)
    except Exception as exc:
        log(f"⚠️ Canonical AI registry read failed: {exc}")
    return domains


def prepare_xray_workspace(date_str):
    workspace = workspace_for_date(date_str)
    os.makedirs(workspace, exist_ok=True)

    raw_source = raw_file_for_date(date_str)
    raw_target = os.path.join(workspace, "domain-names.txt")

    if not raw_source:
        return False, "MISSING"

    if not os.path.exists(raw_target):
        shutil.copy2(raw_source, raw_target)
        log(f"📥 Raw input copied into queue workspace for {date_str}.")
    else:
        log(f"ℹ️ Existing queue workspace preserved for {date_str}.")

    return True, "READY"


def run_filter_and_xray(date_str, run_start):
    workspace = workspace_for_date(date_str)
    os.makedirs(workspace, exist_ok=True)

    write_active_date(date_str)

    filter_output = os.path.join(workspace, "premium_domains.txt")
    xray_done = os.path.join(workspace, "filter_2.done")
    xray_csv = os.path.join(workspace, "Ultimate_God_Leads.csv")

    # LEVEL 1
    if not os.path.exists(filter_output):
        if not can_start_stage(run_start, f"Filter 1 ({date_str})"):
            return "PAUSED"
        timeout = stage_timeout(run_start, FILTER_1_TIMEOUT)
        log(f"STEP X1: Running Domain Filter for {date_str} (timeout={timeout}s)...")
        try:
            run_stage(FILTER_1_SCRIPT, workspace, timeout)
        except subprocess.TimeoutExpired:
            log("⏱️ Filter 1 timed out. Workspace preserved for next run.")
            return "PAUSED"
        except subprocess.CalledProcessError as exc:
            log(f"❌ Filter 1 failed with exit code {exc.returncode}.")
            return "ERROR"
        except OSError as exc:
            log(f"❌ Could not launch Filter 1: {exc}")
            return "ERROR"
    else:
        log(f"STEP X1: {date_str} already filtered — skipping.")

    # LEVEL 2
    if not os.path.exists(xray_done):
        if not can_start_stage(run_start, f"X-Ray ({date_str})"):
            return "PAUSED"
        timeout = stage_timeout(run_start, FILTER_2_TIMEOUT)
        log(f"STEP X2: Running X-Ray for {date_str} (timeout={timeout}s)...")
        try:
            run_stage(FILTER_2_SCRIPT, workspace, timeout)
        except subprocess.TimeoutExpired:
            log("⏱️ X-Ray timed out. Its cache/workspace remains resumable.")
            return "PAUSED"
        except subprocess.CalledProcessError as exc:
            log(f"❌ X-Ray failed with exit code {exc.returncode}.")
            return "ERROR"
        except OSError as exc:
            log(f"❌ Could not launch X-Ray: {exc}")
            return "ERROR"
    else:
        log(f"STEP X2: {date_str} already X-Rayed — skipping.")

    if not os.path.exists(xray_done):
        log("⚠️ X-Ray did not produce filter_2.done. Date remains resumable.")
        return "PAUSED"

    if not valid_csv(xray_csv):
        log("⚠️ X-Ray completion flag exists but CSV is missing/invalid.")
        return "ERROR"

    log(
        f"✅ X-Ray queue source ready: {date_str} "
        f"({os.path.getsize(xray_csv):,} bytes)"
    )
    return "SUCCESS"


def available_xray_dates():
    dates = []
    for date_str in all_raw_dates():
        if os.path.exists(source_xray_done(date_str)) and valid_csv(
            source_xray_file(date_str)
        ):
            dates.append(date_str)
    return dates


def build_pending_ai_queue():
    """Build one global AI input from every completed X-Ray date."""
    canonical_domains = load_canonical_domains()
    all_rows = []
    output_fields = []
    seen_pending = set()

    for date_str in available_xray_dates():
        xray_path = source_xray_file(date_str)
        try:
            rows, fields = load_rows(xray_path)
        except Exception as exc:
            log(f"⚠️ Could not read X-Ray queue {date_str}: {exc}")
            continue

        if not rows:
            continue

        for field in fields:
            if field not in output_fields:
                output_fields.append(field)

        for row in rows:
            domain = normalize_domain(row.get("Domain", ""))
            if not domain:
                continue
            if domain in canonical_domains or domain in seen_pending:
                continue

            row["Domain"] = domain
            row["_Source_Date"] = date_str
            if "_Source_Date" not in output_fields:
                output_fields.append("_Source_Date")

            seen_pending.add(domain)
            all_rows.append(row)

    if not output_fields:
        output_fields = ["Domain", "_Source_Date"]

    with open(
        AI_QUEUE_BUILD_TMP,
        "w",
        encoding="utf-8",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=output_fields,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(all_rows)
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(AI_QUEUE_BUILD_TMP, AI_ENGINE_INPUT)

    return len(all_rows), len(canonical_domains), output_fields


def merge_ai_results_into_canonical():
    """Promote engine final/partial output into the persistent canonical registry."""
    source_path = None
    if valid_csv(AI_ENGINE_PARTIAL):
        source_path = AI_ENGINE_PARTIAL
    elif valid_csv(AI_ENGINE_FINAL):
        source_path = AI_ENGINE_FINAL

    if not source_path:
        return 0

    rows, fields = load_rows(source_path)
    if not rows:
        return 0

    # Prefer the engine file as the canonical registry directly after atomic replace.
    temp = AI_CANONICAL_PARTIAL + ".tmp"
    with open(temp, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, AI_CANONICAL_PARTIAL)

    if source_path != AI_CANONICAL_PARTIAL:
        safe_remove(source_path)

    # The categorizer final means "all rows currently supplied" were processed.
    # Rename nothing here: canonical partial remains the long-lived resume registry.
    return len(rows)


def clear_engine_work_files():
    safe_remove(AI_ENGINE_FINAL)
    # Keep AI_ENGINE_PARTIAL only if it was not promoted; normally it is promoted above.
    safe_remove(AI_ENGINE_PARTIAL)


def run_ai_queue(run_start):
    pending_count, completed_count, _ = build_pending_ai_queue()
    log(
        f"🤖 GLOBAL AI QUEUE: pending={pending_count:,} | "
        f"canonical_completed={completed_count:,}"
    )

    if pending_count == 0:
        return "NO_PENDING"

    if not can_start_stage(run_start, "AI Categorizer"):
        return "PAUSED"

    # Make the engine's partial registry start from the canonical registry.
    # This preserves all historical successful classifications between runs.
    if os.path.exists(AI_CANONICAL_PARTIAL):
        if not os.path.exists(AI_ENGINE_PARTIAL) or os.path.getsize(AI_ENGINE_PARTIAL) == 0:
            shutil.copy2(AI_CANONICAL_PARTIAL, AI_ENGINE_PARTIAL)

    timeout = stage_timeout(run_start, FILTER_3_TIMEOUT)
    log(f"STEP AI: Running Groq categorizer against global queue (timeout={timeout}s)...")

    try:
        run_stage(FILTER_3_SCRIPT, AI_ENGINE_DIR, timeout)
    except subprocess.TimeoutExpired:
        log("⏱️ AI Categorizer timed out. Partial progress will be promoted.")
    except subprocess.CalledProcessError as exc:
        log(
            f"⚠️ AI Categorizer exited {exc.returncode}; "
            "promoting any partial progress and pausing this run."
        )
    except OSError as exc:
        log(f"❌ Could not launch AI Categorizer: {exc}")
        return "ERROR"

    merged = merge_ai_results_into_canonical()
    log(f"✅ Canonical AI registry now contains {merged:,} rows.")

    # If the engine produced a complete final file, it was promoted above and removed.
    # Rebuild the queue once to know whether anything remains.
    remaining, _, _ = build_pending_ai_queue()
    if remaining == 0:
        log("🎉 Global AI queue fully classified.")
        return "SUCCESS"

    log(f"⏸️ Global AI queue still has {remaining:,} unresolved leads.")
    return "PAUSED"


def write_user_facing_archive(rows, fields, date_str):
    archive_fields = [field for field in fields if field != "_Source_Date"]
    target = final_archive_for_date(date_str)
    temp = target + ".tmp"

    with open(temp, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=archive_fields,
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)


def archive_completed_dates():
    if not os.path.exists(AI_CANONICAL_PARTIAL):
        return 0

    canonical_rows, fields = load_rows(AI_CANONICAL_PARTIAL)
    by_date = {}
    for row in canonical_rows:
        date_str = (row.get("_Source_Date") or "").strip()
        domain = normalize_domain(row.get("Domain", ""))
        if date_str and domain:
            by_date.setdefault(date_str, []).append(row)

    archived = 0
    for date_str in available_xray_dates():
        archive_path = final_archive_for_date(date_str)
        if valid_csv(archive_path):
            continue

        source_rows, _ = load_rows(source_xray_file(date_str))
        source_domains = {
            normalize_domain(row.get("Domain", ""))
            for row in source_rows
            if normalize_domain(row.get("Domain", ""))
        }

        classified_rows = by_date.get(date_str, [])
        classified_domains = {
            normalize_domain(row.get("Domain", ""))
            for row in classified_rows
            if normalize_domain(row.get("Domain", ""))
        }

        if source_domains and not source_domains.issubset(classified_domains):
            continue

        # Zero-lead X-Ray file is considered complete too.
        write_user_facing_archive(
            classified_rows,
            fields,
            date_str,
        )
        archived += 1
        log(
            f"🎉 DATE CLOSED: {date_str} → "
            f"{os.path.basename(archive_path)}"
        )

    return archived


def cleanup_closed_workspace(date_str):
    """Keep the X-Ray source CSV for audit/queue purposes, remove only transient work files."""
    workspace = workspace_for_date(date_str)
    if not os.path.isdir(workspace):
        return

    for name in (
        "domain-names.txt",
        "premium_domains.txt",
        "premium_domain_report.txt",
        "scanned_cache.txt",
        "filter_2.done",
    ):
        path = os.path.join(workspace, name)
        if os.path.exists(path):
            safe_remove(path)

    try:
        remaining = os.listdir(workspace)
    except OSError:
        return

    # Keep Ultimate_God_Leads.csv as the durable queue/audit source.
    if remaining == ["Ultimate_God_Leads.csv"]:
        return


def pick_next_xray_date():
    for date_str in all_raw_dates():
        if valid_csv(final_archive_for_date(date_str)):
            continue
        if os.path.exists(source_xray_done(date_str)) and valid_csv(
            source_xray_file(date_str)
        ):
            continue
        return date_str
    return None


def process_one_xray_date(run_start):
    active = read_active_date()
    if active:
        date_str = active
        log(f"🛑 RESUME X-RAY DATE: {date_str}")
    else:
        date_str = pick_next_xray_date()
        if not date_str:
            return "NO_DATE"
        log(f"🎯 NEXT X-RAY DATE: {date_str}")

    ok, state = prepare_xray_workspace(date_str)
    if not ok:
        log(f"ℹ️ Raw data for {date_str} is currently missing upstream.")
        clear_active_date()
        return "MISSING"

    result = run_filter_and_xray(date_str, run_start)
    if result == "SUCCESS":
        clear_active_date()
        log(f"✅ X-Ray date complete: {date_str}")
        return "SUCCESS"

    return result


def run_raw_sync(run_start):
    if not can_start_stage(run_start, "WhoisDS raw sync"):
        return "PAUSED"

    timeout = stage_timeout(run_start, SNIPER_TIMEOUT)
    log("RAW SYNC: Refreshing WhoisDS history independently of all processing queues...")
    try:
        run_stage(SNIPER_SCRIPT, BASE_DIR, timeout)
        log("✅ Raw WhoisDS sync finished.")
        return "SUCCESS"
    except subprocess.TimeoutExpired:
        log("⏱️ Raw sync timed out; processing queues remain untouched.")
        return "PAUSED"
    except subprocess.CalledProcessError as exc:
        log(f"❌ Raw sync failed with exit code {exc.returncode}.")
        return "ERROR"
    except OSError as exc:
        log(f"❌ Could not launch raw sync: {exc}")
        return "ERROR"


def main():
    log("============================================================")
    log("BAWA MASTER CONTROLLER v3.0 ONLINE")
    log("Queue-based / decoupled ingestion / resumable AI")
    log("============================================================")

    run_start = time.time()

    # One-time migration so the first queue-based run does not discard the
    # currently active legacy 2026-07-30 state/progress.
    migrate_legacy_state()

    if not check_internet():
        log("⚠️ Internet unavailable — exiting without touching queue state.")
        return

    # --------------------------------------------------------
    # PHASE 1: ALWAYS HARVEST RAW DATA
    # --------------------------------------------------------
    raw_result = run_raw_sync(run_start)
    if raw_result not in {"SUCCESS", "PAUSED"}:
        log(f"⚠️ Raw sync returned {raw_result}; continuing with already-known data.")

    # --------------------------------------------------------
    # PHASE 2: ADVANCE ONE X-RAY DATE WHEN BUDGET ALLOWS
    # --------------------------------------------------------
    xray_result = process_one_xray_date(run_start)
    if xray_result in {"PAUSED", "ERROR"}:
        log(
            f"⏸️ X-Ray stage returned {xray_result}. "
            "No same-run retry of the same date."
        )
    elif xray_result == "SUCCESS":
        log("✅ One X-Ray date successfully added to the global AI queue.")

    # --------------------------------------------------------
    # PHASE 3: GLOBAL AI QUEUE
    # --------------------------------------------------------
    if remaining_budget(run_start) >= MIN_STAGE_SECONDS:
        ai_result = run_ai_queue(run_start)
        if ai_result == "PAUSED":
            log("⏸️ AI quota/budget prevented full queue completion; state preserved.")
        elif ai_result == "SUCCESS":
            log("✅ AI queue caught up completely.")
        elif ai_result == "NO_PENDING":
            log("ℹ️ No unresolved AI leads currently waiting.")
        else:
            log(f"⚠️ AI queue returned {ai_result}.")
    else:
        log("⏱️ Not enough budget left to start AI queue safely.")

    # --------------------------------------------------------
    # PHASE 4: CLOSE ANY DATES THAT ARE NOW FULLY CLASSIFIED
    # --------------------------------------------------------
    archive_completed_dates()

    log("============================================================")
    log("Master Controller v3.0 run complete. Exiting.")
    log("============================================================")


if __name__ == "__main__":
    main()
