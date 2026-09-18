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
# BAWA MASTER CONTROLLER v4.2
# ============================================================
#
# MAJOR FIX IN v4.2:
#
#   X-Ray completion is now DURABLY CHECKPOINTED TO GIT
#   BEFORE the controller enters the AI queue.
#
# Old failure pattern:
#
#   X-Ray 100%
#       ↓
#   AI claim_batch()
#       ↓
#   Git error / quota / crash
#       ↓
#   X-Ray files remain only on runner
#       ↓
#   next GitHub run sees old state
#       ↓
#   X-Ray starts again
#
# New pattern:
#
#   X-Ray 100%
#       ↓
#   persist_xray_state()
#       ↓
#   Git commit + push
#       ↓
#   AI claim_batch()
#
# Therefore an AI-stage failure cannot destroy an already
# completed X-Ray stage.
#
# Also:
#   - Active-date state is durable.
#   - Filter-1 state is durable.
#   - Partial X-Ray state is checkpointed on timeout/error.
#   - X-Ray completion can be recovered from scanned_cache.txt
#     when every premium domain is represented there.
#   - Groq worker remains global/shared with Ollama.
#
# Compatible with:
#   domain_sniper.py v2.2
#   1_domain_filter.py v3.1
#   2_deep_xray_scanner.py v3.1
#   3_lead_categorizer.py v3.2 worker-compatible
#   ai_queue_manager.py updated Git-safe version
# ============================================================


# ============================================================
# SCRIPT PATHS
# ============================================================

SNIPER_SCRIPT = os.path.join(
    BASE_DIR,
    "domain_sniper.py",
)

FILTER_1_SCRIPT = os.path.join(
    BASE_DIR,
    "1_domain_filter.py",
)

FILTER_2_SCRIPT = os.path.join(
    BASE_DIR,
    "2_deep_xray_scanner.py",
)

FILTER_3_SCRIPT = os.path.join(
    BASE_DIR,
    "3_lead_categorizer.py",
)

PYTHON = sys.executable


# ============================================================
# RUNTIME CONFIG
# ============================================================

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


# ============================================================
# DURABLE WORKSPACE
# ============================================================

ACTIVE_DATE_FILE = os.path.join(
    PROCESSING_QUEUE_DIR,
    "active_date.txt",
)

DAILY_DOMAINS_DIR = os.path.join(
    BASE_DIR,
    "daily_domains",
)

MASTER_CONTROL_ROOM_DIR = os.path.join(
    BASE_DIR,
    "master_control_room",
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
# INTERNET
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
# BUDGET
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
    remaining = int(
        remaining_budget(
            run_start
        )
    )

    return max(
        1,
        min(
            configured,
            remaining,
        ),
    )


# ============================================================
# PROCESS EXECUTION
# ============================================================

def run_script(
    script_path,
    cwd,
    timeout,
    env=None,
):
    return subprocess.run(
        [PYTHON, script_path],
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
    os.makedirs(
        PROCESSING_QUEUE_DIR,
        exist_ok=True,
    )

    temp = (
        ACTIVE_DATE_FILE
        + ".tmp"
    )

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
# RAW DATE DISCOVERY
# ============================================================

def raw_file_for_date(
    date_str,
):
    if not os.path.isdir(
        DAILY_DOMAINS_DIR
    ):
        return None

    matches = []

    for filename in os.listdir(
        DAILY_DOMAINS_DIR
    ):

        if (
            filename.lower().endswith(
                ".txt"
            )
            and date_str in filename
        ):
            matches.append(
                os.path.join(
                    DAILY_DOMAINS_DIR,
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

    if not os.path.isdir(
        DAILY_DOMAINS_DIR
    ):
        return []

    for filename in os.listdir(
        DAILY_DOMAINS_DIR
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

        date_str = match.group(0)

        try:
            datetime.strptime(
                date_str,
                "%Y-%m-%d",
            )

            dates.add(
                date_str
            )

        except ValueError:
            pass

    return sorted(
        dates
    )


# ============================================================
# DATE WORKSPACE PATHS
# ============================================================

def workspace_for_date(
    date_str,
):
    return os.path.join(
        PROCESSING_QUEUE_DIR,
        date_str,
    )


def premium_file(
    date_str,
):
    return os.path.join(
        workspace_for_date(
            date_str
        ),
        "premium_domains.txt",
    )


def premium_report_file(
    date_str,
):
    return os.path.join(
        workspace_for_date(
            date_str
        ),
        "premium_domain_report.txt",
    )


def domain_input_file(
    date_str,
):
    return os.path.join(
        workspace_for_date(
            date_str
        ),
        "domain-names.txt",
    )


def xray_file(
    date_str,
):
    return os.path.join(
        workspace_for_date(
            date_str
        ),
        "Ultimate_God_Leads.csv",
    )


def xray_done(
    date_str,
):
    return os.path.join(
        workspace_for_date(
            date_str
        ),
        "filter_2.done",
    )


def xray_cache_file(
    date_str,
):
    return os.path.join(
        workspace_for_date(
            date_str
        ),
        "scanned_cache.txt",
    )


# ============================================================
# WORKSPACE PREPARATION
# ============================================================

def prepare_xray_workspace(
    date_str,
):
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

    raw_target = domain_input_file(
        date_str
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
# GIT HELPERS FOR X-RAY CHECKPOINT
# ============================================================

def git_command(
    *args,
    check=True,
    capture=True,
):
    proc = subprocess.run(
        ["git", *args],
        cwd=BASE_DIR,
        text=True,
        capture_output=capture,
        check=False,
    )

    if (
        check
        and proc.returncode != 0
    ):

        stderr = (
            proc.stderr.strip()
            if proc.stderr
            else ""
        )

        stdout = (
            proc.stdout.strip()
            if proc.stdout
            else ""
        )

        raise RuntimeError(
            f"git {' '.join(args)} "
            f"failed with {proc.returncode}: "
            f"{stderr or stdout}"
        )

    return proc


def ensure_controller_git_identity():
    current_name = git_command(
        "config",
        "--get",
        "user.name",
        check=False,
    )

    current_email = git_command(
        "config",
        "--get",
        "user.email",
        check=False,
    )

    if not (
        current_name.returncode == 0
        and (current_name.stdout or "").strip()
    ):
        git_command(
            "config",
            "user.name",
            os.environ.get(
                "GIT_USER_NAME",
                "Bawa Cloud Engine",
            ),
        )

    if not (
        current_email.returncode == 0
        and (current_email.stdout or "").strip()
    ):
        git_command(
            "config",
            "user.email",
            os.environ.get(
                "GIT_USER_EMAIL",
                "bawa-cloud-engine@users.noreply.github.com",
            ),
        )


def git_branch():
    branch = os.environ.get(
        "GITHUB_REF_NAME",
        "",
    ).strip()

    if branch:
        return branch

    proc = git_command(
        "symbolic-ref",
        "--short",
        "HEAD",
        check=False,
    )

    if proc.returncode == 0:
        value = (
            proc.stdout or ""
        ).strip()

        if value:
            return value

    return "main"


def git_has_staged_changes():
    proc = git_command(
        "diff",
        "--cached",
        "--quiet",
        check=False,
    )

    return proc.returncode != 0


def persist_xray_state(
    reason,
):
    """
    CRITICAL DURABILITY CHECKPOINT.

    Only controller-owned paths are committed here:

        daily_domains/
        processing_queue/

    ai_queue/ is intentionally excluded because claims/results
    are coordinated by ai_queue_manager.py.
    """

    ensure_controller_git_identity()

    branch = git_branch()

    try:
        # ----------------------------------------------------
        # FIRST REFRESH REMOTE KNOWLEDGE
        # ----------------------------------------------------
        git_command(
            "fetch",
            "origin",
            branch,
        )

        # ----------------------------------------------------
        # STAGE ONLY CONTROLLER-OWNED STATE
        # ----------------------------------------------------
        git_command(
            "add",
            "-A",
            "--",
            "daily_domains",
            "processing_queue",
        )

        if not git_has_staged_changes():
            log(
                f"💾 Checkpoint not needed — "
                f"no Git changes ({reason})."
            )

            return True

        # ----------------------------------------------------
        # COMMIT LOCAL X-RAY STATE
        # ----------------------------------------------------
        message = (
            f"💾 X-Ray checkpoint: "
            f"{reason}"
        )

        git_command(
            "commit",
            "-m",
            message,
        )

        # ----------------------------------------------------
        # PUSH WITH REBASE RETRIES
        # ----------------------------------------------------
        last_error = None

        for attempt in range(
            1,
            5,
        ):

            try:
                git_command(
                    "push",
                    "origin",
                    f"HEAD:{branch}",
                )

                log(
                    f"✅ Durable X-Ray checkpoint pushed "
                    f"successfully ({reason})."
                )

                return True

            except Exception as exc:
                last_error = exc

                log(
                    f"⚠️ Checkpoint push attempt "
                    f"{attempt}/4 failed: {exc}"
                )

                if attempt >= 4:
                    break

                # Remote may have received AI queue commits.
                # Rebase our controller-only state on top.
                try:
                    git_command(
                        "fetch",
                        "origin",
                        branch,
                    )

                    git_command(
                        "rebase",
                        f"origin/{branch}",
                    )

                except Exception as rebase_exc:
                    git_command(
                        "rebase",
                        "--abort",
                        check=False,
                    )

                    log(
                        f"⚠️ Checkpoint rebase failed: "
                        f"{rebase_exc}"
                    )

                time.sleep(
                    1.5 * attempt
                )

        raise RuntimeError(
            str(last_error)
            if last_error
            else "X-Ray checkpoint push failed."
        )

    except Exception as exc:
        log(
            f"❌ DURABLE X-RAY CHECKPOINT FAILED: "
            f"{exc}"
        )

        return False


# ============================================================
# PREMIUM DOMAIN LOADING
# ============================================================

def load_premium_domains(
    date_str,
):
    path = premium_file(
        date_str
    )

    if not os.path.exists(
        path
    ):
        return set()

    domains = set()

    try:
        with open(
            path,
            "r",
            encoding="utf-8",
        ) as handle:

            for line in handle:

                domain = normalize_domain(
                    line
                )

                if domain:
                    domains.add(
                        domain
                    )

    except OSError:
        return set()

    return domains


# ============================================================
# X-RAY CACHE LOADING
# ============================================================

def load_scanned_cache_domains(
    date_str,
):
    """
    Supports:

        SUCCESS|domain.com
        DEAD|domain.com

    and old format:

        domain.com
    """

    path = xray_cache_file(
        date_str
    )

    if not os.path.exists(
        path
    ):
        return set()

    scanned = set()

    try:
        with open(
            path,
            "r",
            encoding="utf-8",
        ) as handle:

            for raw_line in handle:

                line = (
                    raw_line.strip()
                )

                if not line:
                    continue

                if "|" in line:
                    prefix, value = (
                        line.split(
                            "|",
                            1,
                        )
                    )

                    if prefix.upper() in {
                        "SUCCESS",
                        "DEAD",
                    }:
                        line = value.strip()

                domain = normalize_domain(
                    line
                )

                if domain:
                    scanned.add(
                        domain
                    )

    except OSError:
        return set()

    return scanned


# ============================================================
# X-RAY COMPLETION RECOVERY
# ============================================================

def xray_completion_proven(
    date_str,
):
    """
    If every premium domain is represented in scanned_cache.txt,
    the X-Ray stage is logically complete even if filter_2.done
    was not committed during an older failed run.

    This is safe because the cache records BOTH:
        SUCCESS
        DEAD

    so absence from Ultimate_God_Leads.csv does not mean an
    unprocessed domain.
    """

    premium = load_premium_domains(
        date_str
    )

    if not premium:
        return False

    scanned = load_scanned_cache_domains(
        date_str
    )

    if not scanned:
        return False

    missing = premium - scanned

    log(
        f"🩺 X-Ray recovery check: "
        f"premium={len(premium):,}, "
        f"scanned={len(scanned):,}, "
        f"missing={len(missing):,}"
    )

    return not missing


def recover_xray_done_marker(
    date_str,
):
    done = xray_done(
        date_str
    )

    if os.path.exists(
        done
    ):
        return False

    if not os.path.exists(
        xray_file(date_str)
    ):
        return False

    if not xray_completion_proven(
        date_str
    ):
        return False

    temp = (
        done
        + ".tmp"
    )

    try:
        with open(
            temp,
            "w",
            encoding="utf-8",
        ) as handle:

            handle.write(
                "RECOVERED_FROM_COMPLETE_SCANNED_CACHE\n"
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
            f"♻️ X-Ray completion marker RECOVERED "
            f"for {date_str} from scanned_cache.txt."
        )

        return True

    except OSError as exc:
        log(
            f"⚠️ Could not recover X-Ray marker: "
            f"{exc}"
        )

        return False


# ============================================================
# ONE X-RAY DATE
# ============================================================

def run_one_xray_date(
    run_start,
):
    active = read_active_date()

    if active:
        date_str = active

        log(
            f"🛑 RESUME X-RAY DATE: "
            f"{date_str}"
        )

    else:
        date_str = None

        for candidate in all_raw_dates():

            if (
                os.path.exists(
                    xray_done(candidate)
                )
                and os.path.exists(
                    xray_file(candidate)
                )
            ):
                continue

            final_archive = os.path.join(
                MASTER_CONTROL_ROOM_DIR,
                f"Final_Extracted_Leads_{candidate}.csv",
            )

            if os.path.exists(
                final_archive
            ):
                continue

            date_str = candidate
            break

        if not date_str:
            return "NO_DATE"

        log(
            f"🎯 NEXT X-RAY DATE: "
            f"{date_str}"
        )

    # --------------------------------------------------------
    # LOCK DATE
    # --------------------------------------------------------
    write_active_date(
        date_str
    )

    # Make the active-date lock itself durable.
    persist_xray_state(
        f"active date locked {date_str}"
    )

    # --------------------------------------------------------
    # PREPARE WORKSPACE
    # --------------------------------------------------------
    if not prepare_xray_workspace(
        date_str
    ):

        log(
            f"ℹ️ Raw data for {date_str} "
            f"is not available locally."
        )

        clear_active_date()

        persist_xray_state(
            f"clear missing raw date {date_str}"
        )

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

    # --------------------------------------------------------
    # FILTER 1
    # --------------------------------------------------------
    if not os.path.exists(
        filt
    ):

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

            # CRITICAL:
            # Persist premium domains before X-Ray starts.
            persist_xray_state(
                f"Filter 1 complete {date_str}"
            )

        except subprocess.TimeoutExpired:

            log(
                f"⏱️ Filter 1 timed out for "
                f"{date_str}."
            )

            persist_xray_state(
                f"Filter 1 timeout {date_str}"
            )

            return "PAUSED"

        except subprocess.CalledProcessError as exc:

            log(
                f"❌ Filter 1 failed for "
                f"{date_str}: exit={exc.returncode}"
            )

            persist_xray_state(
                f"Filter 1 failed {date_str}"
            )

            return "ERROR"

    else:

        log(
            f"STEP X1: {date_str} "
            f"already filtered — skipping."
        )

    # --------------------------------------------------------
    # TRY COMPLETION RECOVERY BEFORE RUNNING SCANNER
    # --------------------------------------------------------
    if not os.path.exists(
        done
    ):

        recovered = (
            recover_xray_done_marker(
                date_str
            )
        )

        if recovered:

            persist_xray_state(
                f"recover X-Ray completion {date_str}"
            )

    # --------------------------------------------------------
    # X-RAY
    # --------------------------------------------------------
    if not os.path.exists(
        done
    ):

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
                f"⏱️ X-Ray timed out for "
                f"{date_str}."
            )

            # SAVE WHATEVER THE SCANNER PRODUCED.
            persist_xray_state(
                f"X-Ray timeout {date_str}"
            )

            return "PAUSED"

        except subprocess.CalledProcessError as exc:

            log(
                f"❌ X-Ray failed for "
                f"{date_str}: exit={exc.returncode}"
            )

            # SAVE PARTIAL CACHE / OUTPUT.
            persist_xray_state(
                f"X-Ray error {date_str}"
            )

            return "ERROR"

    else:

        log(
            f"STEP X2: {date_str} "
            f"already X-Rayed — skipping."
        )

    # --------------------------------------------------------
    # FINAL X-RAY VALIDATION
    # --------------------------------------------------------
    if not os.path.exists(
        done
    ):

        # Scanner may have finished but marker may be missing.
        if (
            os.path.exists(
                xray_file(date_str)
            )
            and xray_completion_proven(
                date_str
            )
        ):

            recover_xray_done_marker(
                date_str
            )

    if not (
        os.path.exists(
            done
        )
        and os.path.exists(
            xray_file(date_str)
        )
    ):

        # Even if incomplete, checkpoint whatever is present.
        persist_xray_state(
            f"X-Ray incomplete {date_str}"
        )

        return "PAUSED"

    # --------------------------------------------------------
    # CRITICAL DURABILITY CHECKPOINT
    # --------------------------------------------------------
    log(
        f"💾 X-Ray 100% complete for "
        f"{date_str}. Persisting BEFORE AI..."
    )

    checkpoint_ok = persist_xray_state(
        f"X-Ray COMPLETE {date_str}"
    )

    if not checkpoint_ok:

        log(
            "🛑 X-Ray data could NOT be pushed "
            "to GitHub safely."
        )

        log(
            "🛑 AI queue will NOT start for this run."
        )

        return "CHECKPOINT_FAILED"

    # --------------------------------------------------------
    # ONLY AFTER SUCCESSFUL PUSH:
    # CLEAR ACTIVE DATE
    # --------------------------------------------------------
    clear_active_date()

    checkpoint_ok = persist_xray_state(
        f"clear active date after X-Ray {date_str}"
    )

    if not checkpoint_ok:

        log(
            "⚠️ Active-date clear could not be pushed, "
            "but X-Ray data is already durable."
        )

    log(
        f"✅ X-Ray date complete: "
        f"{date_str}"
    )

    log(
        "✅ X-Ray date is ready for "
        "the global AI queue."
    )

    return "SUCCESS"


# ============================================================
# RAW WHOISDS SYNC
# ============================================================

def run_raw_sync(
    run_start,
):
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

        # Raw downloads are also durable immediately.
        persist_xray_state(
            "WhoisDS raw sync"
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

        persist_xray_state(
            "WhoisDS timeout"
        )

        return "PAUSED"

    except subprocess.CalledProcessError as exc:

        log(
            f"❌ Raw sync failed: "
            f"exit={exc.returncode}"
        )

        persist_xray_state(
            "WhoisDS error"
        )

        return "ERROR"


# ============================================================
# LEGACY STATE MIGRATION
# ============================================================

def migrate_legacy_state():
    legacy_dir = os.path.join(
        BASE_DIR,
        "state",
    )

    legacy_lock = os.path.join(
        legacy_dir,
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

        legacy_files = (
            "domain-names.txt",
            "premium_domains.txt",
            "premium_domain_report.txt",
            "Ultimate_God_Leads.csv",
            "scanned_cache.txt",
            "filter_2.done",
        )

        for name in legacy_files:

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
                row["_Source_Date"] = date_str

            canonical_rows, canonical_fields = load_csv(
                CANONICAL_FILE
            )

            by_domain = {}

            for row in canonical_rows:

                domain = normalize_domain(
                    row.get(
                        "Domain",
                        "",
                    )
                )

                if domain:
                    row["Domain"] = domain
                    by_domain[domain] = row

            for row in rows:

                domain = normalize_domain(
                    row.get(
                        "Domain",
                        "",
                    )
                )

                if not domain:
                    continue

                row["Domain"] = domain

                if domain not in by_domain:
                    by_domain[domain] = row

            ordered = list(
                canonical_fields
            )

            for field in (
                fields
                + ["_Source_Date"]
            ):

                if field not in ordered:
                    ordered.append(
                        field
                    )

            from ai_queue_manager import (
                write_csv_atomic,
            )

            write_csv_atomic(
                CANONICAL_FILE,
                list(
                    by_domain.values()
                ),
                ordered,
            )

            log(
                f"   ✅ Migrated legacy AI output "
                f"from {name}: {len(rows):,} rows"
            )

        # Remove migrated legacy temporary files.
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
            f"✅ Legacy state migration "
            f"complete for {date_str}."
        )

    except Exception as exc:

        log(
            f"⚠️ Legacy migration "
            f"encountered an error: {exc}"
        )


# ============================================================
# GROQ WORKER
# ============================================================

def run_groq_worker(
    run_start,
):
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
            "ℹ️ No currently unclaimed "
            "AI leads for Groq."
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

    env[
        "CATEGORIZER_INPUT_FILE"
    ] = os.path.basename(
        input_file
    )

    env[
        "CATEGORIZER_OUTPUT_FILE"
    ] = os.path.basename(
        output_file
    )

    env[
        "CATEGORIZER_PARTIAL_FILE"
    ] = os.path.basename(
        partial_file
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
            f"{exc.returncode}; "
            f"publishing partial results if any."
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

        count = (
            publish_result_and_release(
                claim,
                produced,
            )
        )

        log(
            f"✅ Groq worker released "
            f"claim {claim['claim_id']}; "
            f"published {count:,} result rows."
        )

    except Exception as exc:

        log(
            f"❌ Could not publish/release "
            f"Groq claim "
            f"{claim['claim_id']}: {exc}"
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
        "BAWA MASTER CONTROLLER v4.2 ONLINE"
    )

    log(
        "Durable X-Ray Checkpoint + "
        "Completion Recovery + Global AI Queue"
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
    # NETWORK
    # --------------------------------------------------------
    if not check_internet():

        log(
            "⚠️ Internet unavailable — "
            "exiting without touching queue state."
        )

        return

    # --------------------------------------------------------
    # QUEUE HOUSEKEEPING
    # --------------------------------------------------------
    cleanup_expired_claims()

    merged, total = (
        merge_result_shards()
    )

    if merged:

        log(
            f"✅ Merged {merged:,} new AI results. "
            f"Canonical total={total:,}."
        )

    # --------------------------------------------------------
    # RAW WHOIS SYNC
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
    # ONE X-RAY DATE
    # --------------------------------------------------------
    xray_result = run_one_xray_date(
        run_start
    )

    if xray_result in {
        "PAUSED",
        "ERROR",
        "CHECKPOINT_FAILED",
    }:

        log(
            f"⏸️ X-Ray returned "
            f"{xray_result}; "
            f"AI worker will not force a retry."
        )

    # --------------------------------------------------------
    # GLOBAL GROQ WORKER
    # --------------------------------------------------------
    #
    # The worker starts only AFTER X-Ray state has been
    # durably checkpointed.
    #
    while (
        remaining_budget(
            run_start
        )
        >= MIN_STAGE_SECONDS
    ):

        before = time.time()

        try:

            result = run_groq_worker(
                run_start
            )

        except Exception as exc:

            log(
                f"❌ Groq worker controller error: "
                f"{exc}"
            )

            break

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
            remaining_budget(
                run_start
            )
            < MIN_STAGE_SECONDS
        ):
            break

    # --------------------------------------------------------
    # MERGE AI RESULTS
    # --------------------------------------------------------
    merged, total = (
        merge_result_shards()
    )

    if merged:

        log(
            f"✅ Final merge this run: "
            f"{merged:,} new AI results; "
            f"canonical total={total:,}."
        )

    # --------------------------------------------------------
    # ARCHIVE
    # --------------------------------------------------------
    archived = archive_ready_dates(
        MASTER_CONTROL_ROOM_DIR
    )

    if archived:

        log(
            f"🎉 Archived "
            f"{archived:,} completed date(s)."
        )

    # --------------------------------------------------------
    # FINAL DURABILITY CHECKPOINT
    # --------------------------------------------------------
    #
    # X-Ray state is already independently persisted.
    # This final checkpoint also captures:
    #
    #   daily_domains
    #   processing_queue
    #   generated master_control_room output
    #
    persist_xray_state(
        "controller final checkpoint"
    )

    log(
        "============================================================"
    )

    log(
        "Master Controller v4.2 run complete. Exiting."
    )

    log(
        "============================================================"
    )


if __name__ == "__main__":
    main()
