import csv
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime

from ai_queue_manager import (
    BASE_DIR,
    CANONICAL_FILE,
    PROCESSING_QUEUE_DIR,
    AI_QUEUE_DIR,
    archive_ready_dates,
    claim_batch,
    cleanup_expired_claims,
    load_csv,
    merge_result_shards,
    normalize_domain,
    write_claim_input,
    publish_result_and_release,
)

# ============================================================
# BAWA MASTER CONTROLLER v4.1
# X-Ray completion recovery + Global AI Queue + Groq Worker
#
# Compatible with:
#   domain_sniper.py v2.2
#   1_domain_filter.py v3.1
#   2_deep_xray_scanner.py v3.1
#   3_lead_categorizer.py v3.2
#
# Main fix:
#   A missing filter_2.done no longer automatically means
#   "run X-Ray again".
#
#   If scanned_cache.txt proves that every premium domain
#   was already classified as SUCCESS or DEAD, the controller
#   automatically recreates filter_2.done and skips X-Ray.
# ============================================================

SNIPER_SCRIPT = os.path.join(BASE_DIR, "domain_sniper.py")
FILTER_1_SCRIPT = os.path.join(BASE_DIR, "1_domain_filter.py")
FILTER_2_SCRIPT = os.path.join(BASE_DIR, "2_deep_xray_scanner.py")
FILTER_3_SCRIPT = os.path.join(BASE_DIR, "3_lead_categorizer.py")

PYTHON = sys.executable

MAX_RUNTIME_SECONDS = int(
    os.environ.get(
        "MAX_RUNTIME_SECONDS",
        5 * 3600 + 30 * 60,
    )
)

SAFETY_RESERVE_SECONDS = int(
    os.environ.get(
        "SAFETY_RESERVE_SECONDS",
        60,
    )
)

MIN_STAGE_SECONDS = int(
    os.environ.get(
        "MIN_STAGE_SECONDS",
        120,
    )
)

SNIPER_TIMEOUT = int(
    os.environ.get(
        "SNIPER_TIMEOUT",
        30 * 60,
    )
)

FILTER_1_TIMEOUT = int(
    os.environ.get(
        "FILTER_1_TIMEOUT",
        10 * 60,
    )
)

FILTER_2_TIMEOUT = int(
    os.environ.get(
        "FILTER_2_TIMEOUT",
        2 * 60 * 60,
    )
)

GROQ_TIMEOUT = int(
    os.environ.get(
        "GROQ_TIMEOUT",
        4 * 60 * 60,
    )
)

GROQ_CLAIM_SIZE = int(
    os.environ.get(
        "GROQ_CLAIM_SIZE",
        50,
    )
)

ACTIVE_DATE_FILE = os.path.join(
    PROCESSING_QUEUE_DIR,
    "active_date.txt",
)

for directory in (
    PROCESSING_QUEUE_DIR,
    AI_QUEUE_DIR,
):
    os.makedirs(
        directory,
        exist_ok=True,
    )


# ============================================================
# LOGGING
# ============================================================

def log(message):
    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"{message}",
        flush=True,
    )


# ============================================================
# NETWORK
# ============================================================

def check_internet():
    try:
        socket.create_connection(
            ("1.1.1.1", 53),
            timeout=3,
        )
        return True

    except OSError:
        return False


# ============================================================
# TIME BUDGET
# ============================================================

def remaining_budget(run_start):
    return (
        MAX_RUNTIME_SECONDS
        - (time.time() - run_start)
        - SAFETY_RESERVE_SECONDS
    )


def can_start_stage(
    run_start,
    stage_name,
    required_seconds=MIN_STAGE_SECONDS,
):
    remaining = remaining_budget(
        run_start
    )

    if remaining < required_seconds:
        log(
            f"⏱️ Not enough budget for {stage_name}: "
            f"remaining≈{max(0, int(remaining))}s, "
            f"required={required_seconds}s."
        )
        return False

    return True


def stage_timeout(
    run_start,
    configured,
):
    return max(
        1,
        min(
            configured,
            int(
                remaining_budget(
                    run_start
                )
            ),
        ),
    )


# ============================================================
# PROCESS RUNNER
# ============================================================

def run_script(
    script_path,
    cwd,
    timeout,
    env=None,
):
    return subprocess.run(
        [
            PYTHON,
            script_path,
        ],
        cwd=cwd,
        timeout=timeout,
        check=True,
        env=env,
    )


# ============================================================
# ACTIVE DATE
# ============================================================

def read_active_date():
    try:
        if os.path.exists(
            ACTIVE_DATE_FILE
        ):
            with open(
                ACTIVE_DATE_FILE,
                "r",
                encoding="utf-8",
            ) as handle:
                value = handle.read().strip()

                return value or None

    except OSError as exc:
        log(
            f"⚠️ Active date read failed: {exc}"
        )

    return None


def write_active_date(date_str):
    temp = ACTIVE_DATE_FILE + ".tmp"

    with open(
        temp,
        "w",
        encoding="utf-8",
    ) as handle:

        handle.write(
            date_str + "\n"
        )

        handle.flush()
        os.fsync(
            handle.fileno()
        )

    os.replace(
        temp,
        ACTIVE_DATE_FILE,
    )


def clear_active_date():
    try:
        if os.path.exists(
            ACTIVE_DATE_FILE
        ):
            os.remove(
                ACTIVE_DATE_FILE
            )

    except OSError as exc:
        log(
            f"⚠️ Could not clear active date: {exc}"
        )


# ============================================================
# RAW DATA
# ============================================================

def raw_file_for_date(date_str):

    raw_dir = os.path.join(
        BASE_DIR,
        "daily_domains",
    )

    if not os.path.isdir(
        raw_dir
    ):
        return None

    matches = []

    for filename in os.listdir(
        raw_dir
    ):

        if (
            filename.lower().endswith(".txt")
            and date_str in filename
        ):
            matches.append(
                os.path.join(
                    raw_dir,
                    filename,
                )
            )

    return (
        sorted(matches)[0]
        if matches
        else None
    )


def all_raw_dates():

    dates = set()

    raw_dir = os.path.join(
        BASE_DIR,
        "daily_domains",
    )

    if not os.path.isdir(
        raw_dir
    ):
        return []

    for filename in os.listdir(
        raw_dir
    ):

        if not filename.lower().endswith(
            ".txt"
        ):
            continue

        match = re.search(
            r"\d{4}-\d{2}-\d{2}",
            filename,
        )

        if not match:
            continue

        try:
            datetime.strptime(
                match.group(0),
                "%Y-%m-%d",
            )

            dates.add(
                match.group(0)
            )

        except ValueError:
            pass

    return sorted(dates)


# ============================================================
# DATE WORKSPACE
# ============================================================

def workspace_for_date(date_str):
    return os.path.join(
        PROCESSING_QUEUE_DIR,
        date_str,
    )


def xray_file(date_str):
    return os.path.join(
        workspace_for_date(date_str),
        "Ultimate_God_Leads.csv",
    )


def xray_done(date_str):
    return os.path.join(
        workspace_for_date(date_str),
        "filter_2.done",
    )


def xray_cache_file(date_str):
    return os.path.join(
        workspace_for_date(date_str),
        "scanned_cache.txt",
    )


def premium_file(date_str):
    return os.path.join(
        workspace_for_date(date_str),
        "premium_domains.txt",
    )


def prepare_xray_workspace(date_str):

    workspace = workspace_for_date(
        date_str
    )

    os.makedirs(
        workspace,
        exist_ok=True,
    )

    raw_source = raw_file_for_date(
        date_str
    )

    raw_target = os.path.join(
        workspace,
        "domain-names.txt",
    )

    if not raw_source:
        return False

    if not os.path.exists(
        raw_target
    ):

        shutil.copy2(
            raw_source,
            raw_target,
        )

        log(
            f"📥 Copied raw input for "
            f"{date_str} into processing queue."
        )

    return True


# ============================================================
# X-RAY COMPLETION RECOVERY
# ============================================================

def load_premium_domains(date_str):

    path = premium_file(
        date_str
    )

    if not os.path.exists(path):
        return set()

    domains = set()

    try:
        with open(
            path,
            "r",
            encoding="utf-8-sig",
        ) as handle:

            for line in handle:

                domain = normalize_domain(
                    line.strip()
                )

                if domain:
                    domains.add(
                        domain
                    )

    except OSError as exc:

        log(
            f"⚠️ Could not read premium "
            f"domain list for {date_str}: {exc}"
        )

        return set()

    return domains


def load_scanned_cache_domains(date_str):

    path = xray_cache_file(
        date_str
    )

    if not os.path.exists(path):
        return set()

    scanned = set()

    try:
        with open(
            path,
            "r",
            encoding="utf-8-sig",
        ) as handle:

            for raw_line in handle:

                line = raw_line.strip()

                if not line:
                    continue

                # Current format:
                # SUCCESS|domain.com
                # DEAD|domain.com
                #
                # Backward-compatible:
                # domain.com

                if "|" in line:

                    _, value = line.split(
                        "|",
                        1,
                    )

                    domain = normalize_domain(
                        value
                    )

                else:

                    domain = normalize_domain(
                        line
                    )

                if domain:
                    scanned.add(
                        domain
                    )

    except OSError as exc:

        log(
            f"⚠️ Could not read X-Ray cache "
            f"for {date_str}: {exc}"
        )

        return set()

    return scanned


def xray_completion_proven(date_str):

    done = xray_done(
        date_str
    )

    xray = xray_file(
        date_str
    )

    # Normal expected state.
    if (
        os.path.exists(done)
        and os.path.exists(xray)
    ):
        return True

    premium = load_premium_domains(
        date_str
    )

    scanned = load_scanned_cache_domains(
        date_str
    )

    # Without premium list and cache,
    # we cannot safely prove completion.
    if not premium:
        return False

    if not scanned:
        return False

    # The scanner is considered complete only when
    # every domain that entered X-Ray is represented
    # in the terminal cache as SUCCESS or DEAD.
    missing = premium - scanned

    if missing:
        return False

    # We have proof that every premium domain was terminally
    # processed. Restore the completion marker if needed.
    if not os.path.exists(done):

        try:

            temp = done + ".tmp"

            with open(
                temp,
                "w",
                encoding="utf-8",
            ) as handle:

                handle.write(
                    "RECOVERED\n"
                )

                handle.flush()
                os.fsync(
                    handle.fileno()
                )

            os.replace(
                temp,
                done,
            )

            log(
                f"🛡️ X-Ray completion recovered "
                f"from scanned_cache.txt for {date_str} "
                f"({len(scanned):,}/{len(premium):,} domains)."
            )

        except OSError as exc:

            log(
                f"⚠️ Could not recreate X-Ray "
                f"completion marker for {date_str}: {exc}"
            )

            return False

    return os.path.exists(done)


def xray_state_is_complete(date_str):

    done = xray_done(
        date_str
    )

    xray = xray_file(
        date_str
    )

    # Strongest evidence.
    if (
        os.path.exists(done)
        and os.path.exists(xray)
    ):
        return True

    # Recovery path.
    return xray_completion_proven(
        date_str
    )


# ============================================================
# X-RAY DATE PROCESSING
# ============================================================

def run_one_xray_date(run_start):

    active = read_active_date()

    if active:

        date_str = active

        log(
            f"🛑 RESUME X-RAY DATE: {date_str}"
        )

        # IMPORTANT:
        # A stale active_date.txt must never force a
        # second full X-Ray if the previous run actually
        # completed the scan.
        if xray_state_is_complete(
            date_str
        ):

            clear_active_date()

            log(
                f"✅ Existing X-Ray completion confirmed "
                f"for {date_str} — full rescan avoided."
            )

            return "SUCCESS"

    else:

        date_str = None

        for candidate in all_raw_dates():

            # Skip fully completed X-Ray dates.
            if xray_state_is_complete(
                candidate
            ):

                continue

            archive = os.path.join(
                BASE_DIR,
                "master_control_room",
                f"Final_Extracted_Leads_{candidate}.csv",
            )

            if os.path.exists(
                archive
            ):
                continue

            date_str = candidate

            break

        if not date_str:
            return "NO_DATE"

        log(
            f"🎯 NEXT X-RAY DATE: {date_str}"
        )

    write_active_date(
        date_str
    )

    if not prepare_xray_workspace(
        date_str
    ):

        log(
            f"ℹ️ Raw data for {date_str} "
            f"is not available locally."
        )

        clear_active_date()

        return "MISSING"

    workspace = workspace_for_date(
        date_str
    )

    filt = premium_file(
        date_str
    )

    done = xray_done(
        date_str
    )

    xray = xray_file(
        date_str
    )

    # ========================================================
    # FILTER 1
    # ========================================================

    if not os.path.exists(filt):

        if not can_start_stage(
            run_start,
            f"Filter 1 ({date_str})",
            FILTER_1_TIMEOUT,
        ):
            return "PAUSED"

        try:

            run_script(
                FILTER_1_SCRIPT,
                workspace,
                stage_timeout(
                    run_start,
                    FILTER_1_TIMEOUT,
                ),
            )

        except subprocess.TimeoutExpired:

            return "PAUSED"

        except subprocess.CalledProcessError as exc:

            log(
                f"❌ Filter 1 failed for "
                f"{date_str}: exit={exc.returncode}"
            )

            return "ERROR"

    else:

        log(
            f"STEP X1: {date_str} "
            f"already filtered — skipping."
        )

    # ========================================================
    # RE-CHECK AFTER FILTER 1
    # ========================================================

    # This is the critical protection against repeating
    # a completed X-Ray when only filter_2.done was missing.

    if xray_state_is_complete(
        date_str
    ):

        clear_active_date()

        log(
            f"✅ X-Ray already complete for "
            f"{date_str} — skipping full rescan."
        )

        return "SUCCESS"

    # ========================================================
    # X-RAY
    # ========================================================

    if not os.path.exists(done):

        if not can_start_stage(
            run_start,
            f"X-Ray ({date_str})",
            FILTER_2_TIMEOUT,
        ):
            return "PAUSED"

        try:

            run_script(
                FILTER_2_SCRIPT,
                workspace,
                stage_timeout(
                    run_start,
                    FILTER_2_TIMEOUT,
                ),
            )

        except subprocess.TimeoutExpired:

            log(
                f"⏱️ X-Ray timed out for {date_str}; "
                f"state remains resumable."
            )

            return "PAUSED"

        except subprocess.CalledProcessError as exc:

            log(
                f"❌ X-Ray failed for "
                f"{date_str}: exit={exc.returncode}"
            )

            return "ERROR"

    else:

        log(
            f"STEP X2: {date_str} "
            f"already X-Rayed — skipping."
        )

    # ========================================================
    # FINAL X-RAY COMPLETION CHECK
    # ========================================================

    # Normal scanner completion.
    if (
        os.path.exists(done)
        and os.path.exists(xray)
    ):

        clear_active_date()

        log(
            f"✅ X-Ray date complete: {date_str}"
        )

        return "SUCCESS"

    # Recovery check after scanner execution.
    if xray_state_is_complete(
        date_str
    ):

        clear_active_date()

        log(
            f"✅ X-Ray date complete and "
            f"completion marker recovered: {date_str}"
        )

        return "SUCCESS"

    log(
        f"⚠️ X-Ray output is not proven complete "
        f"for {date_str}; keeping date resumable."
    )

    return "PAUSED"


# ============================================================
# RAW WHOIS SYNC
# ============================================================

def run_raw_sync(run_start):

    if not can_start_stage(
        run_start,
        "WhoisDS raw sync",
        SNIPER_TIMEOUT,
    ):
        return "PAUSED"

    try:

        log(
            "RAW SYNC: Refreshing WhoisDS "
            "history independently of AI queue..."
        )

        run_script(
            SNIPER_SCRIPT,
            BASE_DIR,
            stage_timeout(
                run_start,
                SNIPER_TIMEOUT,
            ),
        )

        log(
            "✅ Raw WhoisDS sync finished."
        )

        return "SUCCESS"

    except subprocess.TimeoutExpired:

        log(
            "⏱️ Raw sync timed out; "
            "existing raw data remains intact."
        )

        return "PAUSED"

    except subprocess.CalledProcessError as exc:

        log(
            f"❌ Raw sync failed: "
            f"exit={exc.returncode}"
        )

        return "ERROR"


# ============================================================
# LEGACY STATE MIGRATION
# ============================================================

def migrate_legacy_state():

    legacy_lock = os.path.join(
        BASE_DIR,
        "state",
        "date_lock.txt",
    )

    if not os.path.exists(
        legacy_lock
    ):
        return

    try:

        with open(
            legacy_lock,
            "r",
            encoding="utf-8",
        ) as handle:

            date_str = handle.read().strip()

        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}",
            date_str,
        ):
            return

        workspace = workspace_for_date(
            date_str
        )

        os.makedirs(
            workspace,
            exist_ok=True,
        )

        legacy_dir = os.path.join(
            BASE_DIR,
            "state",
        )

        for name in (
            "domain-names.txt",
            "premium_domains.txt",
            "premium_domain_report.txt",
            "Ultimate_God_Leads.csv",
            "scanned_cache.txt",
            "filter_2.done",
        ):

            src = os.path.join(
                legacy_dir,
                name,
            )

            dst = os.path.join(
                workspace,
                name,
            )

            if (
                os.path.exists(src)
                and not os.path.exists(dst)
            ):

                shutil.copy2(
                    src,
                    dst,
                )

                log(
                    f"   ✅ Migrated {name}"
                )

        # Legacy categorized output migration.
        for name in (
            "Bawa_Categorized_Leads.partial.csv",
            "Bawa_Categorized_Leads.csv",
        ):

            src = os.path.join(
                legacy_dir,
                name,
            )

            if not os.path.exists(
                src
            ):
                continue

            rows, fields = load_csv(
                src
            )

            if not rows:
                continue

            for row in rows:

                row["_Source_Date"] = (
                    date_str
                )

            from ai_queue_manager import write_csv_atomic

            canonical_rows, canonical_fields = (
                load_csv(
                    CANONICAL_FILE
                )
            )

            by_domain = {
                normalize_domain(
                    r.get("Domain", "")
                ): r
                for r in canonical_rows
                if normalize_domain(
                    r.get("Domain", "")
                )
            }

            for row in rows:

                domain = normalize_domain(
                    row.get("Domain", "")
                )

                if domain:

                    row["Domain"] = domain

                    by_domain.setdefault(
                        domain,
                        row,
                    )

            ordered = list(
                canonical_fields
            )

            for field in fields + [
                "_Source_Date"
            ]:

                if field not in ordered:
                    ordered.append(
                        field
                    )

            write_csv_atomic(
                CANONICAL_FILE,
                list(
                    by_domain.values()
                ),
                ordered,
            )

            log(
                f"   ✅ Migrated legacy AI "
                f"output from {name}: "
                f"{len(rows):,} rows"
            )

        for name in (
            "Bawa_Categorized_Leads.partial.csv",
            "Bawa_Categorized_Leads.csv",
            "date_lock.txt",
        ):

            path = os.path.join(
                legacy_dir,
                name,
            )

            try:

                if os.path.exists(
                    path
                ):
                    os.remove(
                        path
                    )

            except OSError:
                pass

        log(
            f"✅ Legacy state migration complete "
            f"for {date_str}."
        )

    except Exception as exc:

        log(
            f"⚠️ Legacy migration encountered "
            f"an error: {exc}"
        )


# ============================================================
# GROQ WORKER
# ============================================================

def run_groq_worker(run_start):

    if not can_start_stage(
        run_start,
        "Groq AI worker",
        min(
            MIN_STAGE_SECONDS,
            GROQ_TIMEOUT,
        ),
    ):
        return "PAUSED"

    claim = claim_batch(
        "groq",
        GROQ_CLAIM_SIZE,
    )

    if not claim:

        log(
            "ℹ️ No currently unclaimed AI "
            "leads for Groq."
        )

        return "NO_WORK"

    input_file = write_claim_input(
        claim
    )

    work_dir = claim[
        "work_dir"
    ]

    output_file = os.path.join(
        work_dir,
        "result.csv",
    )

    partial_file = os.path.join(
        work_dir,
        "partial.csv",
    )

    env = os.environ.copy()

    env["CATEGORIZER_INPUT_FILE"] = (
        os.path.basename(
            input_file
        )
    )

    env["CATEGORIZER_OUTPUT_FILE"] = (
        os.path.basename(
            output_file
        )
    )

    env["CATEGORIZER_PARTIAL_FILE"] = (
        os.path.basename(
            partial_file
        )
    )

    env.setdefault(
        "GROQ_BATCH_SIZE",
        "5",
    )

    try:

        log(
            f"🤖 GROQ WORKER: claimed "
            f"{len(claim['rows']):,} leads "
            f"(claim={claim['claim_id']})"
        )

        run_script(
            FILTER_3_SCRIPT,
            work_dir,
            stage_timeout(
                run_start,
                GROQ_TIMEOUT,
            ),
            env=env,
        )

    except subprocess.TimeoutExpired:

        log(
            "⏱️ Groq worker timed out; "
            "will publish whatever partial "
            "result exists."
        )

    except subprocess.CalledProcessError as exc:

        log(
            f"⚠️ Groq worker exited "
            f"{exc.returncode}; publishing "
            f"partial results if any."
        )

    except OSError as exc:

        log(
            f"❌ Could not launch Groq worker: "
            f"{exc}"
        )

        return "ERROR"

    produced = (
        output_file
        if os.path.exists(
            output_file
        )
        else partial_file
    )

    try:

        count = publish_result_and_release(
            claim,
            produced,
        )

        log(
            f"✅ Groq worker released "
            f"claim {claim['claim_id']}; "
            f"published {count:,} result rows."
        )

    except Exception as exc:

        log(
            f"❌ Could not publish/release "
            f"Groq claim {claim['claim_id']}: "
            f"{exc}"
        )

        return "ERROR"

    return (
        "SUCCESS"
        if count
        else "PAUSED"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    log(
        "============================================================"
    )

    log(
        "BAWA MASTER CONTROLLER v4.1 ONLINE"
    )

    log(
        "X-Ray Completion Recovery + Global AI Queue + Groq Worker"
    )

    log(
        "============================================================"
    )

    run_start = time.time()

    # --------------------------------------------------------
    # LEGACY MIGRATION
    # --------------------------------------------------------

    migrate_legacy_state()

    # --------------------------------------------------------
    # INTERNET
    # --------------------------------------------------------

    if not check_internet():

        log(
            "⚠️ Internet unavailable — "
            "exiting without touching queue state."
        )

        return

    # --------------------------------------------------------
    # RECOVER / MERGE EXISTING AI RESULTS
    # --------------------------------------------------------

    cleanup_expired_claims()

    merged, total = merge_result_shards()

    if merged:

        log(
            f"✅ Merged {merged:,} new AI results. "
            f"Canonical total={total:,}."
        )

    # --------------------------------------------------------
    # PHASE 1: RAW SYNC
    # --------------------------------------------------------

    raw_result = run_raw_sync(
        run_start
    )

    if raw_result == "ERROR":

        log(
            "⚠️ Raw sync failed; "
            "continuing with already-known raw data."
        )

    # --------------------------------------------------------
    # PHASE 2: ONE X-RAY DATE
    # --------------------------------------------------------

    xray_result = run_one_xray_date(
        run_start
    )

    if xray_result in {
        "PAUSED",
        "ERROR",
    }:

        log(
            f"⏸️ X-Ray returned "
            f"{xray_result}; "
            f"no same-run retry."
        )

    elif xray_result == "SUCCESS":

        log(
            "✅ X-Ray date is ready "
            "for the global AI queue."
        )

    elif xray_result == "NO_DATE":

        log(
            "ℹ️ No X-Ray date currently "
            "requires processing."
        )

    # --------------------------------------------------------
    # PHASE 3: GLOBAL GROQ AI QUEUE
    # --------------------------------------------------------

    while (
        remaining_budget(run_start)
        >= MIN_STAGE_SECONDS
    ):

        before = time.time()

        result = run_groq_worker(
            run_start
        )

        after = time.time()

        if result in {
            "NO_WORK",
            "ERROR",
            "PAUSED",
        }:
            break

        if after - before < 2:
            break

        if (
            remaining_budget(run_start)
            < MIN_STAGE_SECONDS
        ):
            break

    # --------------------------------------------------------
    # PHASE 4: MERGE AI RESULT SHARDS
    # --------------------------------------------------------

    merge_result_shards()

    # --------------------------------------------------------
    # PHASE 5: ARCHIVE COMPLETED DATES
    # --------------------------------------------------------

    archived = archive_ready_dates(
        os.path.join(
            BASE_DIR,
            "master_control_room",
        )
    )

    if archived:

        log(
            f"🎉 Archived "
            f"{archived:,} completed date(s)."
        )

    log(
        "============================================================"
    )

    log(
        "Master Controller v4.1 run complete. Exiting."
    )

    log(
        "============================================================"
    )


if __name__ == "__main__":
    main()
