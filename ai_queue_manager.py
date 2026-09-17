import csv
import hashlib
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone


# ============================================================
# BAWA AI QUEUE MANAGER
# Git-backed Global AI Queue
#
# Compatible with:
#   - master_controller.py v4.x
#   - Groq worker
#   - Ollama worker
#
# IMPORTANT DESIGN:
#   1) X-Ray output lives in processing_queue/
#   2) AI claims live in ai_queue/claims/
#   3) AI result shards live in ai_queue/results/
#   4) Canonical AI completion lives in
#      ai_queue/completed_registry.csv
#   5) Git is the durable coordination layer
#
# SAFETY FIX:
#   Multiple runners/workers can update Git while this process
#   is still working.
#
#   Therefore git_sync_before_claim():
#       fetches remote
#       reconciles identical local/remote state
#       preserves genuinely local state
#       then pulls with rebase
#
#   This prevents errors like:
#       "untracked working tree files would be overwritten by merge"
# ============================================================


# ============================================================
# PATHS
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PROCESSING_QUEUE_DIR = os.path.join(
    BASE_DIR,
    "processing_queue",
)

AI_QUEUE_DIR = os.path.join(
    BASE_DIR,
    "ai_queue",
)

CLAIMS_DIR = os.path.join(
    AI_QUEUE_DIR,
    "claims",
)

RESULTS_DIR = os.path.join(
    AI_QUEUE_DIR,
    "results",
)

WORK_DIR = os.path.join(
    AI_QUEUE_DIR,
    "work",
)

CANONICAL_FILE = os.path.join(
    AI_QUEUE_DIR,
    "completed_registry.csv",
)


# ============================================================
# CONFIG
# ============================================================

CLAIM_TTL_SECONDS = int(
    os.environ.get(
        "AI_CLAIM_TTL_SECONDS",
        12 * 3600,
    )
)

GIT_USER_NAME = os.environ.get(
    "GIT_USER_NAME",
    "Bawa Cloud Engine",
)

GIT_USER_EMAIL = os.environ.get(
    "GIT_USER_EMAIL",
    "bawa-cloud-engine@users.noreply.github.com",
)


# ============================================================
# DURABLE STATE ROOTS
#
# Only these pipeline-generated directories are automatically
# checkpointed/reconciled by the Git sync layer.
# ============================================================

STATE_ROOTS = (
    "daily_domains",
    "processing_queue",
    "ai_queue",
    "master_control_room",
    "state",
)


# ============================================================
# DIRECTORY INITIALIZATION
# ============================================================

def ensure_queue_dirs():
    """
    Ensure all runtime queue directories exist.

    Git does not track empty directories, so these must always
    be recreated at runtime.
    """
    for path in (
        PROCESSING_QUEUE_DIR,
        AI_QUEUE_DIR,
        CLAIMS_DIR,
        RESULTS_DIR,
        WORK_DIR,
    ):
        os.makedirs(path, exist_ok=True)


ensure_queue_dirs()


# ============================================================
# GENERAL HELPERS
# ============================================================

def normalize_domain(value):
    """
    Normalize a domain into a canonical comparable form.
    """
    value = (value or "").strip().lower()

    if "://" in value:
        value = value.split("://", 1)[1]

    value = value.split("/", 1)[0]

    return value.rstrip(".")


def now_ts():
    return int(time.time())


def now_iso():
    return datetime.now(
        timezone.utc
    ).replace(
        microsecond=0
    ).isoformat()


def safe_relpath(path):
    """
    Return repository-relative normalized path.
    """
    rel = os.path.relpath(path, BASE_DIR)
    return rel.replace("\\", "/")


def is_state_path(rel_path):
    """
    Whether a repository-relative path belongs to one of the
    pipeline-managed state roots.
    """
    rel_path = (rel_path or "").replace("\\", "/").lstrip("./")

    for root in STATE_ROOTS:
        if rel_path == root or rel_path.startswith(root + "/"):
            return True

    return False


# ============================================================
# CLAIM / RESULT / WORK PATHS
# ============================================================

def claim_path(domain):
    """
    One claim lock per normalized domain.
    """
    normalized = normalize_domain(domain)

    digest = hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()

    os.makedirs(CLAIMS_DIR, exist_ok=True)

    return os.path.join(
        CLAIMS_DIR,
        f"{digest}.json",
    )


def result_path(worker, claim_id):
    """
    Result shard path for one worker claim.
    """
    safe_worker = "".join(
        c if c.isalnum() or c in "-_" else "_"
        for c in (worker or "worker")
    )

    os.makedirs(RESULTS_DIR, exist_ok=True)

    return os.path.join(
        RESULTS_DIR,
        f"{safe_worker}_{claim_id}.csv",
    )


def work_path(worker, claim_id):
    """
    Temporary/input workspace for one claim.
    """
    safe_worker = "".join(
        c if c.isalnum() or c in "-_" else "_"
        for c in (worker or "worker")
    )

    path = os.path.join(
        WORK_DIR,
        f"{safe_worker}_{claim_id}",
    )

    os.makedirs(path, exist_ok=True)

    return path


# ============================================================
# GIT CORE
# ============================================================

def git(*args, check=True, capture=False):
    """
    Execute a Git command from the repository root.
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=BASE_DIR,
        text=True,
        capture_output=capture,
        check=False,
    )

    if check and proc.returncode != 0:
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

        detail = stderr or stdout

        raise RuntimeError(
            f"git {' '.join(args)} failed "
            f"with {proc.returncode}: {detail}"
        )

    return proc


def ensure_git_identity():
    """
    Ensure Git has a valid author identity.

    GitHub Actions runners frequently start without
    user.name/user.email configured.
    """
    name_proc = git(
        "config",
        "--get",
        "user.name",
        check=False,
        capture=True,
    )

    email_proc = git(
        "config",
        "--get",
        "user.email",
        check=False,
        capture=True,
    )

    current_name = (
        (name_proc.stdout or "").strip()
        if name_proc.returncode == 0
        else ""
    )

    current_email = (
        (email_proc.stdout or "").strip()
        if email_proc.returncode == 0
        else ""
    )

    if not current_name:
        git(
            "config",
            "user.name",
            GIT_USER_NAME,
            check=True,
            capture=True,
        )

    if not current_email:
        git(
            "config",
            "user.email",
            GIT_USER_EMAIL,
            check=True,
            capture=True,
        )


def current_branch():
    """
    Determine the active branch.

    GITHUB_REF_NAME is preferred in GitHub Actions.
    """
    env_branch = os.environ.get(
        "GITHUB_REF_NAME",
        "",
    ).strip()

    if env_branch:
        return env_branch

    proc = git(
        "symbolic-ref",
        "--short",
        "HEAD",
        check=False,
        capture=True,
    )

    if proc.returncode == 0:
        branch = (proc.stdout or "").strip()
        if branch:
            return branch

    return "main"


def remote_ref(branch=None):
    branch = branch or current_branch()
    return f"origin/{branch}"


# ============================================================
# GIT STATUS HELPERS
# ============================================================

def git_changed_paths():
    """
    Return all changed/untracked repository-relative paths.

    Includes:
      - unstaged tracked changes
      - staged changes
      - untracked files
    """
    changed = set()

    commands = [
        (
            "diff",
            "--name-only",
        ),
        (
            "diff",
            "--cached",
            "--name-only",
        ),
        (
            "ls-files",
            "--others",
            "--exclude-standard",
        ),
    ]

    for args in commands:
        proc = git(
            *args,
            check=True,
            capture=True,
        )

        for line in (
            proc.stdout or ""
        ).splitlines():

            path = line.strip()

            if not path:
                continue

            path = path.replace("\\", "/")

            if is_state_path(path):
                changed.add(path)

    return sorted(changed)


def git_path_exists_in_ref(
    ref,
    rel_path,
):
    """
    Check whether a path exists in a Git ref.
    """
    if not rel_path:
        return False

    proc = git(
        "cat-file",
        "-e",
        f"{ref}:{rel_path}",
        check=False,
        capture=True,
    )

    return proc.returncode == 0


def git_blob_hash_local(rel_path):
    """
    Hash a local file using Git's own object hashing.
    """
    full_path = os.path.join(
        BASE_DIR,
        rel_path,
    )

    if not os.path.isfile(full_path):
        return None

    proc = git(
        "hash-object",
        "--",
        rel_path,
        check=True,
        capture=True,
    )

    return (proc.stdout or "").strip()


def git_blob_hash_remote(
    ref,
    rel_path,
):
    """
    Get the Git blob hash of a remote file.
    """
    proc = git(
        "rev-parse",
        f"{ref}:{rel_path}",
        check=False,
        capture=True,
    )

    if proc.returncode != 0:
        return None

    return (proc.stdout or "").strip()


def path_tracked_in_head(rel_path):
    """
    Check whether a path exists in current HEAD.
    """
    return git_path_exists_in_ref(
        "HEAD",
        rel_path,
    )


# ============================================================
# LOCAL STATE RECONCILIATION
# ============================================================

def discard_local_path(rel_path):
    """
    Remove/revert a local pipeline-generated path.

    Used only when the exact same content already exists
    on the remote ref, so keeping the local duplicate is
    unnecessary and would block pull --rebase.
    """
    full_path = os.path.join(
        BASE_DIR,
        rel_path,
    )

    if path_tracked_in_head(rel_path):
        git(
            "restore",
            "--staged",
            "--worktree",
            "--",
            rel_path,
            check=True,
            capture=True,
        )
        return

    # Not tracked by HEAD.
    git(
        "reset",
        "--",
        rel_path,
        check=False,
        capture=True,
    )

    if os.path.isfile(full_path):
        try:
            os.remove(full_path)
        except OSError:
            pass


def reconcile_local_state_with_remote(
    branch,
):
    """
    Reconcile local pipeline state before pulling.

    CASE A:
        Remote path exists and local content is IDENTICAL.
        -> discard local duplicate safely.

    CASE B:
        Remote path does not exist.
        -> preserve local file for checkpoint commit.

    CASE C:
        Remote path exists but content DIFFERENT.
        -> raise a clear conflict instead of silently
           overwriting one worker's work with another worker's.
    """
    ensure_queue_dirs()

    ref = remote_ref(branch)
    changed_paths = git_changed_paths()

    if not changed_paths:
        return {
            "identical_discarded": 0,
            "local_only": 0,
            "conflicts": [],
        }

    identical_discarded = 0
    local_only = 0
    conflicts = []

    for rel_path in changed_paths:

        if not is_state_path(rel_path):
            continue

        local_full = os.path.join(
            BASE_DIR,
            rel_path,
        )

        remote_exists = git_path_exists_in_ref(
            ref,
            rel_path,
        )

        if not remote_exists:
            local_only += 1
            continue

        # Remote has the same path.
        #
        # For directories or unusual Git states, fail safely.
        if not os.path.isfile(local_full):
            conflicts.append(
                rel_path
            )
            continue

        local_hash = git_blob_hash_local(
            rel_path
        )

        remote_hash = git_blob_hash_remote(
            ref,
            rel_path,
        )

        if (
            local_hash
            and remote_hash
            and local_hash == remote_hash
        ):
            discard_local_path(
                rel_path
            )

            identical_discarded += 1

        else:
            conflicts.append(
                rel_path
            )

    return {
        "identical_discarded": identical_discarded,
        "local_only": local_only,
        "conflicts": conflicts,
    }


# ============================================================
# GIT SYNC BEFORE CLAIM
# ============================================================

def checkpoint_local_state():
    """
    Commit remaining local pipeline state changes.

    This function does NOT push.

    The claim transaction will include this checkpoint commit
    and push it together with the claim, or the outer workflow
    can push it later.
    """
    changed_paths = [
        p
        for p in git_changed_paths()
        if is_state_path(p)
    ]

    if not changed_paths:
        return False

    # Add only pipeline state roots.
    add_args = [
        "add",
        "-A",
        "--",
        *STATE_ROOTS,
    ]

    git(
        *add_args,
        check=True,
        capture=True,
    )

    status = git(
        "status",
        "--porcelain",
        check=True,
        capture=True,
    )

    if not (status.stdout or "").strip():
        return False

    git(
        "commit",
        "-m",
        "checkpoint: persist pipeline state before AI claim",
        check=True,
        capture=True,
    )

    return True


def git_sync_before_claim():
    """
    SAFELY synchronize the local repository before taking
    an AI claim.

    Sequence:

        1) ensure directories
        2) ensure Git identity
        3) fetch origin
        4) compare local state against remote
        5) discard only exact duplicates
        6) refuse conflicting divergent state
        7) checkpoint genuinely local state
        8) pull --rebase
        9) recreate runtime directories

    This specifically fixes:
        "untracked working tree files would be overwritten by merge"
    """
    ensure_queue_dirs()
    ensure_git_identity()

    branch = current_branch()
    ref = remote_ref(branch)

    # --------------------------------------------------------
    # FETCH FIRST
    # --------------------------------------------------------
    git(
        "fetch",
        "origin",
        branch,
        check=True,
        capture=True,
    )

    # --------------------------------------------------------
    # RECONCILE LOCAL STATE
    # --------------------------------------------------------
    reconciliation = (
        reconcile_local_state_with_remote(
            branch
        )
    )

    conflicts = reconciliation[
        "conflicts"
    ]

    if conflicts:
        preview = ", ".join(
            conflicts[:10]
        )

        if len(conflicts) > 10:
            preview += (
                f" ... +{len(conflicts) - 10} more"
            )

        raise RuntimeError(
            "Concurrent Git state conflict detected. "
            "Remote already contains the same pipeline path "
            "with DIFFERENT local content. "
            f"Conflicting path(s): {preview}. "
            "Refusing to overwrite data."
        )

    # --------------------------------------------------------
    # CHECKPOINT GENUINELY LOCAL STATE
    # --------------------------------------------------------
    checkpointed = checkpoint_local_state()

    # --------------------------------------------------------
    # PULL REBASE
    # --------------------------------------------------------
    try:
        git(
            "pull",
            "--rebase",
            "origin",
            branch,
            check=True,
            capture=True,
        )

    except Exception:
        # If rebase started but failed, make sure the repository
        # is not left in a half-open rebase state.
        git(
            "rebase",
            "--abort",
            check=False,
            capture=True,
        )

        raise

    ensure_queue_dirs()

    return {
        "branch": branch,
        "remote_ref": ref,
        "checkpointed": checkpointed,
        "identical_discarded": reconciliation[
            "identical_discarded"
        ],
        "local_only": reconciliation[
            "local_only"
        ],
    }


# ============================================================
# GIT PUSH WITH REBASE
# ============================================================

def git_push_with_rebase(
    commit_message,
    max_attempts=4,
):
    """
    Push current clean Git state.

    Used after a claim/result/release commit.

    The working tree should normally be clean here because
    claim/result/release transactions commit their changes
    before calling this function.
    """
    ensure_git_identity()

    last_error = None

    for attempt in range(
        1,
        max_attempts + 1,
    ):
        try:
            git(
                "push",
                check=True,
                capture=True,
            )

            return True

        except Exception as exc:
            last_error = exc

            # Refresh remote information.
            branch = current_branch()

            git(
                "fetch",
                "origin",
                branch,
                check=False,
                capture=True,
            )

            try:
                git(
                    "pull",
                    "--rebase",
                    "origin",
                    branch,
                    check=True,
                    capture=True,
                )

            except Exception:
                git(
                    "rebase",
                    "--abort",
                    check=False,
                    capture=True,
                )

            if attempt < max_attempts:
                time.sleep(
                    1.5 * attempt
                )

    raise RuntimeError(
        str(last_error)
        if last_error
        else "git push failed"
    )


# ============================================================
# CSV HELPERS
# ============================================================

def load_csv(path):
    """
    Load CSV safely.
    """
    if not os.path.exists(path):
        return [], []

    with open(
        path,
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:

        reader = csv.DictReader(
            handle
        )

        rows = list(reader)
        fields = list(
            reader.fieldnames or []
        )

    return rows, fields


def write_csv_atomic(
    path,
    rows,
    fields,
):
    """
    Atomically write CSV.

    Writes to .tmp first, flushes/fsyncs,
    then replaces destination.
    """
    directory = os.path.dirname(path)

    if directory:
        os.makedirs(
            directory,
            exist_ok=True,
        )

    temp = path + ".tmp"

    with open(
        temp,
        "w",
        encoding="utf-8",
        newline="",
    ) as handle:

        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(rows)

        handle.flush()
        os.fsync(handle.fileno())

    os.replace(
        temp,
        path,
    )


# ============================================================
# COMPLETED DOMAIN REGISTRY
# ============================================================

def load_completed_domains():
    """
    Return every domain already classified.

    Sources:
        1) canonical registry
        2) result shards

    Result shards are authoritative completion evidence even
    before merge_result_shards() has consumed them.
    """
    completed = set()

    # --------------------------------------------------------
    # CANONICAL
    # --------------------------------------------------------
    rows, _ = load_csv(
        CANONICAL_FILE
    )

    for row in rows:
        domain = normalize_domain(
            row.get("Domain", "")
        )

        if domain:
            completed.add(
                domain
            )

    # --------------------------------------------------------
    # RESULT SHARDS
    # --------------------------------------------------------
    if os.path.isdir(
        RESULTS_DIR
    ):

        for filename in os.listdir(
            RESULTS_DIR
        ):

            if not filename.lower().endswith(
                ".csv"
            ):
                continue

            path = os.path.join(
                RESULTS_DIR,
                filename,
            )

            try:
                rows, _ = load_csv(
                    path
                )

            except Exception:
                continue

            for row in rows:
                domain = normalize_domain(
                    row.get("Domain", "")
                )

                if domain:
                    completed.add(
                        domain
                    )

    return completed


# ============================================================
# ACTIVE CLAIM REGISTRY
# ============================================================

def load_active_claimed_domains():
    """
    Return domains currently protected by non-expired claims.

    Supports BOTH formats:

        old:
            {"domain": "example.com"}

        current:
            {"domains": ["example.com", "..."]}

    This keeps old claim files backward compatible.
    """
    claimed = set()
    now = now_ts()

    ensure_queue_dirs()

    try:
        files = os.listdir(
            CLAIMS_DIR
        )

    except OSError:
        return claimed

    for filename in files:

        if not filename.endswith(
            ".json"
        ):
            continue

        path = os.path.join(
            CLAIMS_DIR,
            filename,
        )

        try:
            with open(
                path,
                "r",
                encoding="utf-8",
            ) as handle:

                payload = json.load(
                    handle
                )

            expires_at = int(
                payload.get(
                    "expires_at",
                    0,
                )
            )

            if expires_at <= now:
                continue

            domains = []

            # Current multi-domain format.
            raw_domains = payload.get(
                "domains",
                [],
            )

            if isinstance(
                raw_domains,
                list,
            ):
                domains.extend(
                    raw_domains
                )

            # Backward-compatible single-domain format.
            legacy_domain = payload.get(
                "domain",
                "",
            )

            if legacy_domain:
                domains.append(
                    legacy_domain
                )

            for value in domains:
                domain = normalize_domain(
                    value
                )

                if domain:
                    claimed.add(
                        domain
                    )

        except Exception:
            continue

    return claimed


# ============================================================
# ALL X-RAY ROWS
# ============================================================

def all_xray_rows():
    """
    Load X-Ray rows from every completed date workspace.

    A date becomes visible to the AI queue only when BOTH:
        - filter_2.done
        - Ultimate_God_Leads.csv
    exist.
    """
    rows = []

    if not os.path.isdir(
        PROCESSING_QUEUE_DIR
    ):
        return rows

    for date_str in sorted(
        os.listdir(
            PROCESSING_QUEUE_DIR
        )
    ):

        workspace = os.path.join(
            PROCESSING_QUEUE_DIR,
            date_str,
        )

        if not os.path.isdir(
            workspace
        ):
            continue

        xray = os.path.join(
            workspace,
            "Ultimate_God_Leads.csv",
        )

        done = os.path.join(
            workspace,
            "filter_2.done",
        )

        if not (
            os.path.exists(done)
            and os.path.exists(xray)
        ):
            continue

        try:
            date_rows, _ = load_csv(
                xray
            )

        except Exception:
            continue

        for row in date_rows:

            domain = normalize_domain(
                row.get("Domain", "")
            )

            if not domain:
                continue

            row["Domain"] = domain
            row["_Source_Date"] = date_str

            rows.append(
                row
            )

    return rows


# ============================================================
# AI CANDIDATES
# ============================================================

def candidate_rows(limit=None):
    """
    Return X-Ray rows which are:

        - not already completed
        - not currently claimed
        - unique by domain
    """
    completed = load_completed_domains()
    claimed = load_active_claimed_domains()

    seen = set(
        completed
    )

    seen.update(
        claimed
    )

    candidates = []

    for row in all_xray_rows():

        domain = normalize_domain(
            row.get("Domain", "")
        )

        if not domain:
            continue

        if domain in seen:
            continue

        seen.add(
            domain
        )

        candidates.append(
            row
        )

        if (
            limit
            and len(candidates) >= limit
        ):
            break

    return candidates


# ============================================================
# CLAIM BATCH
# ============================================================

def claim_batch(
    worker,
    batch_size,
):
    """
    Atomically claim a batch of domains.

    Workflow:

        sync remote
        select free candidates
        create exclusive claim files
        commit claim
        push claim
        return claim metadata

    If another worker wins the race, local claim commit is
    rolled back and selection is retried.
    """
    ensure_queue_dirs()
    ensure_git_identity()

    for attempt in range(
        4
    ):

        # ----------------------------------------------------
        # SYNC BEFORE CLAIM
        # ----------------------------------------------------
        git_sync_before_claim()

        # ----------------------------------------------------
        # SELECT FREE CANDIDATES
        # ----------------------------------------------------
        candidates = candidate_rows(
            limit=batch_size
        )

        if not candidates:
            return None

        claim_id = uuid.uuid4().hex[:12]

        domains = [
            normalize_domain(
                row.get("Domain", "")
            )
            for row in candidates
        ]

        expires_at = (
            now_ts()
            + CLAIM_TTL_SECONDS
        )

        created_files = []

        # Base SHA AFTER sync/checkpoint.
        base_sha_proc = git(
            "rev-parse",
            "HEAD",
            check=True,
            capture=True,
        )

        base_sha = (
            base_sha_proc.stdout or ""
        ).strip()

        try:
            # ------------------------------------------------
            # CREATE EXCLUSIVE CLAIM FILES
            # ------------------------------------------------
            for row in candidates:

                domain = normalize_domain(
                    row.get("Domain", "")
                )

                if not domain:
                    continue

                path = claim_path(
                    domain
                )

                if os.path.exists(
                    path
                ):
                    raise FileExistsError(
                        path
                    )

                # Write BOTH:
                #   domain
                #   domains
                #
                # This keeps old readers and current readers
                # compatible.
                payload = {
                    "claim_id": claim_id,
                    "worker": worker,
                    "domain": domain,
                    "domains": [domain],
                    "created_at": now_ts(),
                    "created_at_iso": now_iso(),
                    "expires_at": expires_at,
                }

                with open(
                    path,
                    "x",
                    encoding="utf-8",
                ) as handle:

                    json.dump(
                        payload,
                        handle,
                        indent=2,
                        ensure_ascii=False,
                    )

                created_files.append(
                    path
                )

            if not created_files:
                return None

            # ------------------------------------------------
            # COMMIT CLAIM
            # ------------------------------------------------
            rel = [
                safe_relpath(path)
                for path in created_files
            ]

            git(
                "add",
                *rel,
                check=True,
                capture=True,
            )

            git(
                "commit",
                "-m",
                f"🔒 AI claim {worker} {claim_id}",
                check=True,
                capture=True,
            )

            # ------------------------------------------------
            # PUSH CLAIM
            # ------------------------------------------------
            try:
                git(
                    "push",
                    check=True,
                    capture=True,
                )

            except Exception:

                # Another worker may have pushed a competing
                # claim/state update first.
                #
                # Return to exact pre-claim SHA and retry.
                git(
                    "reset",
                    "--hard",
                    base_sha,
                    check=False,
                    capture=True,
                )

                # reset --hard does not remove untracked claim
                # files, so clean only the files created here.
                for path in created_files:
                    if os.path.exists(path):
                        try:
                            os.remove(path)
                        except OSError:
                            pass

                if attempt < 3:
                    time.sleep(
                        1.5 * (attempt + 1)
                    )
                    continue

                raise

            # ------------------------------------------------
            # CLAIM SUCCESS
            # ------------------------------------------------
            return {
                "claim_id": claim_id,
                "worker": worker,
                "domains": domains,
                "rows": candidates,
                "claim_files": created_files,
                "work_dir": work_path(
                    worker,
                    claim_id,
                ),
                "result_file": result_path(
                    worker,
                    claim_id,
                ),
                "input_file": os.path.join(
                    work_path(
                        worker,
                        claim_id,
                    ),
                    "input.csv",
                ),
                "expires_at": expires_at,
            }

        except Exception:

            # ------------------------------------------------
            # CLEAN LOCAL CLAIM FILES
            # ------------------------------------------------
            for path in created_files:

                if os.path.exists(
                    path
                ):

                    try:
                        os.remove(
                            path
                        )
                    except OSError:
                        pass

            # ------------------------------------------------
            # RESTORE PRE-CLAIM TREE
            # ------------------------------------------------
            git(
                "reset",
                "--hard",
                base_sha,
                check=False,
                capture=True,
            )

            # reset --hard leaves untracked files behind.
            for path in created_files:
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

            if attempt < 3:
                time.sleep(
                    1.0 * (attempt + 1)
                )
                continue

            raise

    return None


# ============================================================
# CLAIM INPUT
# ============================================================

def write_claim_input(claim):
    """
    Write the claimed X-Ray rows into the categorizer input CSV.
    """
    rows = claim.get(
        "rows",
        [],
    )

    fields = []

    for row in rows:

        for field in row.keys():

            if field not in fields:
                fields.append(
                    field
                )

    if "Domain" not in fields:
        fields.insert(
            0,
            "Domain",
        )

    write_csv_atomic(
        claim["input_file"],
        rows,
        fields,
    )

    return claim[
        "input_file"
    ]


# ============================================================
# PUBLISH RESULT + RELEASE CLAIM
# ============================================================

def publish_result_and_release(
    claim,
    produced_csv,
):
    """
    Publish AI result while claim locks still exist,
    then release claim locks in a separate Git transaction.
    """
    rows = []
    fields = []

    if os.path.exists(
        produced_csv
    ):
        rows, fields = load_csv(
            produced_csv
        )

    # --------------------------------------------------------
    # PUBLISH RESULT
    # --------------------------------------------------------
    if rows:

        write_csv_atomic(
            claim["result_file"],
            rows,
            fields,
        )

        rel_result = safe_relpath(
            claim["result_file"]
        )

        git(
            "add",
            rel_result,
            check=True,
            capture=True,
        )

        git(
            "commit",
            "-m",
            (
                f"✅ AI result "
                f"{claim['worker']} "
                f"{claim['claim_id']}"
            ),
            check=True,
            capture=True,
        )

        git_push_with_rebase(
            (
                f"AI result "
                f"{claim['worker']} "
                f"{claim['claim_id']}"
            )
        )

    # --------------------------------------------------------
    # RELEASE CLAIM LOCKS
    # --------------------------------------------------------
    deleted_claim_files = []

    for path in claim.get(
        "claim_files",
        [],
    ):

        if os.path.exists(
            path
        ):

            try:
                os.remove(
                    path
                )

                deleted_claim_files.append(
                    path
                )

            except OSError:
                pass

    # --------------------------------------------------------
    # COMMIT CLAIM RELEASE
    # --------------------------------------------------------
    if deleted_claim_files:

        remaining_rel = [
            safe_relpath(
                path
            )
            for path in deleted_claim_files
        ]

        git(
            "add",
            *remaining_rel,
            check=True,
            capture=True,
        )

        git(
            "commit",
            "-m",
            (
                f"🔓 AI release "
                f"{claim['worker']} "
                f"{claim['claim_id']}"
            ),
            check=True,
            capture=True,
        )

        git_push_with_rebase(
            (
                f"AI release "
                f"{claim['worker']} "
                f"{claim['claim_id']}"
            )
        )

    return len(rows)


# ============================================================
# MERGE RESULT SHARDS
# ============================================================

def merge_result_shards():
    """
    Merge result shards into the canonical registry.

    Duplicate domains are ignored.

    Result shard files are consumed after successful merge.
    """
    canonical_rows, canonical_fields = load_csv(
        CANONICAL_FILE
    )

    by_domain = {}

    ordered_fields = list(
        canonical_fields
    )

    # --------------------------------------------------------
    # EXISTING CANONICAL ROWS
    # --------------------------------------------------------
    for row in canonical_rows:

        domain = normalize_domain(
            row.get("Domain", "")
        )

        if not domain:
            continue

        row["Domain"] = domain
        by_domain[domain] = row

    merged = 0

    # --------------------------------------------------------
    # RESULT SHARDS
    # --------------------------------------------------------
    if os.path.isdir(
        RESULTS_DIR
    ):

        for filename in sorted(
            os.listdir(
                RESULTS_DIR
            )
        ):

            if not filename.lower().endswith(
                ".csv"
            ):
                continue

            path = os.path.join(
                RESULTS_DIR,
                filename,
            )

            rows, fields = load_csv(
                path
            )

            for field in fields:

                if field not in ordered_fields:
                    ordered_fields.append(
                        field
                    )

            for row in rows:

                domain = normalize_domain(
                    row.get("Domain", "")
                )

                if not domain:
                    continue

                row["Domain"] = domain

                if domain not in by_domain:
                    by_domain[domain] = row
                    merged += 1

            # ------------------------------------------------
            # CONSUME RESULT SHARD
            # ------------------------------------------------
            try:
                os.remove(
                    path
                )

            except OSError:
                pass

    # --------------------------------------------------------
    # DEFAULT FIELDS
    # --------------------------------------------------------
    if not ordered_fields:
        ordered_fields = [
            "Domain",
            "_Source_Date",
        ]

    # --------------------------------------------------------
    # WRITE CANONICAL
    # --------------------------------------------------------
    if by_domain:

        # Preserve stable order by original insertion.
        write_csv_atomic(
            CANONICAL_FILE,
            list(
                by_domain.values()
            ),
            ordered_fields,
        )

    return (
        merged,
        len(by_domain),
    )


# ============================================================
# EXPIRED CLAIM CLEANUP
# ============================================================

def cleanup_expired_claims():
    """
    Delete claim locks whose TTL has expired.

    Returns:
        number of removed claim files
    """
    now = now_ts()
    removed = 0

    ensure_queue_dirs()

    if not os.path.isdir(
        CLAIMS_DIR
    ):
        return 0

    for filename in os.listdir(
        CLAIMS_DIR
    ):

        if not filename.endswith(
            ".json"
        ):
            continue

        path = os.path.join(
            CLAIMS_DIR,
            filename,
        )

        try:
            with open(
                path,
                "r",
                encoding="utf-8",
            ) as handle:

                payload = json.load(
                    handle
                )

            expires_at = int(
                payload.get(
                    "expires_at",
                    0,
                )
            )

            if expires_at <= now:

                os.remove(
                    path
                )

                removed += 1

        except Exception:
            continue

    return removed


# ============================================================
# ARCHIVE READY DATES
# ============================================================

def archive_ready_dates(
    master_dir,
):
    """
    Create Final_Extracted_Leads_<date>.csv only after every
    X-Ray domain from that date has a successful AI result.
    """
    archived = 0

    canonical_rows, fields = load_csv(
        CANONICAL_FILE
    )

    by_date = {}

    # --------------------------------------------------------
    # INDEX CANONICAL RESULTS BY SOURCE DATE
    # --------------------------------------------------------
    for row in canonical_rows:

        date_str = (
            row.get(
                "_Source_Date",
                "",
            )
            or ""
        ).strip()

        domain = normalize_domain(
            row.get(
                "Domain",
                "",
            )
        )

        if not date_str or not domain:
            continue

        by_date.setdefault(
            date_str,
            {},
        )[domain] = row

    if not os.path.isdir(
        PROCESSING_QUEUE_DIR
    ):
        return 0

    os.makedirs(
        master_dir,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # CHECK EACH DATE
    # --------------------------------------------------------
    for date_str in sorted(
        os.listdir(
            PROCESSING_QUEUE_DIR
        )
    ):

        workspace = os.path.join(
            PROCESSING_QUEUE_DIR,
            date_str,
        )

        if not os.path.isdir(
            workspace
        ):
            continue

        source = os.path.join(
            workspace,
            "Ultimate_God_Leads.csv",
        )

        done = os.path.join(
            workspace,
            "filter_2.done",
        )

        archive = os.path.join(
            master_dir,
            f"Final_Extracted_Leads_{date_str}.csv",
        )

        # ----------------------------------------------------
        # X-RAY MUST BE COMPLETE
        # ----------------------------------------------------
        if not (
            os.path.exists(done)
            and os.path.exists(source)
        ):
            continue

        # ----------------------------------------------------
        # ALREADY ARCHIVED
        # ----------------------------------------------------
        if os.path.exists(
            archive
        ):
            continue

        # ----------------------------------------------------
        # GET SOURCE DOMAINS
        # ----------------------------------------------------
        source_rows, _ = load_csv(
            source
        )

        source_domains = {
            normalize_domain(
                row.get(
                    "Domain",
                    "",
                )
            )
            for row in source_rows
            if normalize_domain(
                row.get(
                    "Domain",
                    "",
                )
            )
        }

        classified = by_date.get(
            date_str,
            {},
        )

        # ----------------------------------------------------
        # EVERY X-RAY DOMAIN MUST BE CLASSIFIED
        # ----------------------------------------------------
        if (
            source_domains
            and not source_domains.issubset(
                set(classified)
            )
        ):
            continue

        # ----------------------------------------------------
        # EXCLUDE INTERNAL META FIELDS
        # ----------------------------------------------------
        archive_fields = [
            field
            for field in fields
            if field not in {
                "_Worker",
                "_Processed_At",
                "_Source_Date",
            }
        ]

        out_rows = list(
            classified.values()
        )

        write_csv_atomic(
            archive,
            out_rows,
            archive_fields,
        )

        archived += 1

    return archived


# ============================================================
# PARTIAL RESULT CHECKPOINT
# ============================================================

def publish_result_checkpoint(
    claim,
    produced_csv,
):
    """
    Publish a partial/complete result shard without releasing
    the claim.

    Useful for long-running AI categorization jobs where a
    worker wants to checkpoint progress while keeping its
    domains locked.
    """
    if not os.path.exists(
        produced_csv
    ):
        return 0

    rows, fields = load_csv(
        produced_csv
    )

    if not rows:
        return 0

    write_csv_atomic(
        claim["result_file"],
        rows,
        fields,
    )

    rel = safe_relpath(
        claim["result_file"]
    )

    git(
        "add",
        rel,
        check=True,
        capture=True,
    )

    git(
        "commit",
        "-m",
        (
            f"💾 AI checkpoint "
            f"{claim['worker']} "
            f"{claim['claim_id']}"
        ),
        check=True,
        capture=True,
    )

    git_push_with_rebase(
        (
            f"AI checkpoint "
            f"{claim['worker']} "
            f"{claim['claim_id']}"
        )
    )

    return len(rows)


# ============================================================
# END
# ============================================================
