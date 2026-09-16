import csv
import hashlib
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROCESSING_QUEUE_DIR = os.path.join(BASE_DIR, "processing_queue")
AI_QUEUE_DIR = os.path.join(BASE_DIR, "ai_queue")
CLAIMS_DIR = os.path.join(AI_QUEUE_DIR, "claims")
RESULTS_DIR = os.path.join(AI_QUEUE_DIR, "results")
WORK_DIR = os.path.join(AI_QUEUE_DIR, "work")
CANONICAL_FILE = os.path.join(AI_QUEUE_DIR, "completed_registry.csv")
CLAIM_TTL_SECONDS = int(os.environ.get("AI_CLAIM_TTL_SECONDS", 12 * 3600))

for path in (AI_QUEUE_DIR, CLAIMS_DIR, RESULTS_DIR, WORK_DIR):
    os.makedirs(path, exist_ok=True)


def normalize_domain(value):
    value = (value or "").strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0]
    return value.rstrip(".")


def now_ts():
    return int(time.time())


def claim_path(domain):
    digest = hashlib.sha256(normalize_domain(domain).encode("utf-8")).hexdigest()
    return os.path.join(CLAIMS_DIR, f"{digest}.json")


def result_path(worker, claim_id):
    safe_worker = "".join(c if c.isalnum() or c in "-_" else "_" for c in worker)
    return os.path.join(RESULTS_DIR, f"{safe_worker}_{claim_id}.csv")


def work_path(worker, claim_id):
    safe_worker = "".join(c if c.isalnum() or c in "-_" else "_" for c in worker)
    path = os.path.join(WORK_DIR, f"{safe_worker}_{claim_id}")
    os.makedirs(path, exist_ok=True)
    return path


def git(*args, check=True, capture=False):
    proc = subprocess.run(
        ["git", *args],
        cwd=BASE_DIR,
        text=True,
        capture_output=capture,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed with {proc.returncode}: "
            f"{proc.stderr.strip() if proc.stderr else ''}"
        )
    return proc


def git_sync_before_claim():
    git("pull", "--rebase", check=True, capture=True)


def git_push_with_rebase(commit_message, max_attempts=4):
    last_error = None
    for _ in range(max_attempts):
        try:
            git("push", check=True, capture=True)
            return True
        except Exception as exc:
            last_error = exc
            try:
                git("pull", "--rebase", check=True, capture=True)
            except Exception:
                git("rebase", "--abort", check=False, capture=True)
            time.sleep(1.5)
    raise RuntimeError(str(last_error) if last_error else "git push failed")


def load_csv(path):
    if not os.path.exists(path):
        return [], []
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader), list(reader.fieldnames or [])


def write_csv_atomic(path, rows, fields):
    temp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(temp, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def load_completed_domains():
    completed = set()
    rows, _ = load_csv(CANONICAL_FILE)
    for row in rows:
        domain = normalize_domain(row.get("Domain", ""))
        if domain:
            completed.add(domain)

    # Results are authoritative completion evidence even before the controller
    # has merged them into the canonical registry.
    if os.path.isdir(RESULTS_DIR):
        for filename in os.listdir(RESULTS_DIR):
            if not filename.lower().endswith(".csv"):
                continue
            rows, _ = load_csv(os.path.join(RESULTS_DIR, filename))
            for row in rows:
                domain = normalize_domain(row.get("Domain", ""))
                if domain:
                    completed.add(domain)
    return completed


def load_active_claimed_domains():
    claimed = set()
    now = now_ts()
    try:
        files = os.listdir(CLAIMS_DIR)
    except OSError:
        return claimed

    for filename in files:
        if not filename.endswith(".json"):
            continue
        path = os.path.join(CLAIMS_DIR, filename)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            expires_at = int(payload.get("expires_at", 0))
            domains = [normalize_domain(v) for v in payload.get("domains", [])]
            if expires_at > now:
                claimed.update(d for d in domains if d)
        except Exception:
            continue
    return claimed


def all_xray_rows():
    rows = []
    if not os.path.isdir(PROCESSING_QUEUE_DIR):
        return rows
    for date_str in sorted(os.listdir(PROCESSING_QUEUE_DIR)):
        workspace = os.path.join(PROCESSING_QUEUE_DIR, date_str)
        xray = os.path.join(workspace, "Ultimate_God_Leads.csv")
        done = os.path.join(workspace, "filter_2.done")
        if not (os.path.exists(done) and os.path.exists(xray)):
            continue
        try:
            date_rows, _ = load_csv(xray)
        except Exception:
            continue
        for row in date_rows:
            domain = normalize_domain(row.get("Domain", ""))
            if not domain:
                continue
            row["Domain"] = domain
            row["_Source_Date"] = date_str
            rows.append(row)
    return rows


def candidate_rows(limit=None):
    completed = load_completed_domains()
    claimed = load_active_claimed_domains()
    seen = set(completed)
    seen.update(claimed)
    candidates = []
    for row in all_xray_rows():
        domain = normalize_domain(row.get("Domain", ""))
        if not domain or domain in seen:
            continue
        seen.add(domain)
        candidates.append(row)
        if limit and len(candidates) >= limit:
            break
    return candidates


def claim_batch(worker, batch_size):
    for attempt in range(4):
        git_sync_before_claim()
        candidates = candidate_rows(limit=batch_size)
        if not candidates:
            return None

        claim_id = uuid.uuid4().hex[:12]
        domains = [normalize_domain(row["Domain"]) for row in candidates]
        expires_at = now_ts() + CLAIM_TTL_SECONDS
        created_files = []
        base_sha_proc = git("rev-parse", "HEAD", check=True, capture=True)
        base_sha = base_sha_proc.stdout.strip()

        try:
            for row in candidates:
                domain = normalize_domain(row["Domain"])
                path = claim_path(domain)
                if os.path.exists(path):
                    raise FileExistsError(path)
                payload = {
                    "claim_id": claim_id,
                    "worker": worker,
                    "domain": domain,
                    "created_at": now_ts(),
                    "expires_at": expires_at,
                }
                with open(path, "x", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2)
                created_files.append(path)

            rel = [os.path.relpath(path, BASE_DIR) for path in created_files]
            git("add", *rel, check=True, capture=True)
            git("commit", "-m", f"🔒 AI claim {worker} {claim_id}", check=True, capture=True)
            try:
                git("push", check=True, capture=True)
            except Exception:
                # Another worker may have claimed one of the same domains first.
                # Roll our local claim commit back to the exact pre-claim SHA,
                # then refresh from remote and retry selection.
                git("reset", "--hard", base_sha, check=False, capture=True)
                if attempt < 3:
                    time.sleep(1.5)
                    continue
                raise

            return {
                "claim_id": claim_id,
                "worker": worker,
                "domains": domains,
                "rows": candidates,
                "claim_files": created_files,
                "work_dir": work_path(worker, claim_id),
                "result_file": result_path(worker, claim_id),
                "input_file": os.path.join(work_path(worker, claim_id), "input.csv"),
            }
        except Exception:
            # Remove only files from a claim that never became part of remote.
            for path in created_files:
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            git("reset", "--hard", base_sha, check=False, capture=True)
            if attempt < 3:
                time.sleep(1.0)
                continue
            raise


def write_claim_input(claim):
    rows = claim["rows"]
    fields = []
    for row in rows:
        for field in row.keys():
            if field not in fields:
                fields.append(field)
    if "Domain" not in fields:
        fields.insert(0, "Domain")
    write_csv_atomic(claim["input_file"], rows, fields)
    return claim["input_file"]


def publish_result_and_release(claim, produced_csv):
    rows = []
    fields = []
    if os.path.exists(produced_csv):
        rows, fields = load_csv(produced_csv)

    # Publish result while claim locks still exist.
    if rows:
        write_csv_atomic(claim["result_file"], rows, fields)
        rel_result = os.path.relpath(claim["result_file"], BASE_DIR)
        git("add", rel_result, check=True, capture=True)
        git("commit", "-m", f"✅ AI result {claim['worker']} {claim['claim_id']}", check=True, capture=True)
        git_push_with_rebase(f"AI result {claim['worker']} {claim['claim_id']}")

    # Release claim locks in a second transaction.
    for path in claim["claim_files"]:
        if os.path.exists(path):
            os.remove(path)

    remaining_rel = [os.path.relpath(p, BASE_DIR) for p in claim["claim_files"] if not os.path.exists(p)]
    # Even when no result was produced, deleting the claims is a real change.
    # If a result was published, the result commit is already remote; now release locks.
    if remaining_rel:
        git("add", *remaining_rel, check=True, capture=True)
        git("commit", "-m", f"🔓 AI release {claim['worker']} {claim['claim_id']}", check=True, capture=True)
        git_push_with_rebase(f"AI release {claim['worker']} {claim['claim_id']}")

    return len(rows)


def merge_result_shards():
    canonical_rows, canonical_fields = load_csv(CANONICAL_FILE)
    by_domain = {}
    ordered_fields = list(canonical_fields)

    for row in canonical_rows:
        domain = normalize_domain(row.get("Domain", ""))
        if domain:
            row["Domain"] = domain
            by_domain[domain] = row

    merged = 0
    if os.path.isdir(RESULTS_DIR):
        for filename in sorted(os.listdir(RESULTS_DIR)):
            if not filename.lower().endswith(".csv"):
                continue
            path = os.path.join(RESULTS_DIR, filename)
            rows, fields = load_csv(path)
            for field in fields:
                if field not in ordered_fields:
                    ordered_fields.append(field)
            changed = False
            for row in rows:
                domain = normalize_domain(row.get("Domain", ""))
                if not domain:
                    continue
                row["Domain"] = domain
                if domain not in by_domain:
                    by_domain[domain] = row
                    merged += 1
                    changed = True
            # Result shard is consumed once its rows are incorporated.
            try:
                os.remove(path)
            except OSError:
                pass

    if not ordered_fields:
        ordered_fields = ["Domain", "_Source_Date"]

    if by_domain:
        write_csv_atomic(CANONICAL_FILE, list(by_domain.values()), ordered_fields)

    return merged, len(by_domain)


def cleanup_expired_claims():
    now = now_ts()
    removed = 0
    if not os.path.isdir(CLAIMS_DIR):
        return 0
    for filename in os.listdir(CLAIMS_DIR):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(CLAIMS_DIR, filename)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if int(payload.get("expires_at", 0)) <= now:
                os.remove(path)
                removed += 1
        except Exception:
            continue
    return removed


def archive_ready_dates(master_dir):
    archived = 0
    canonical_rows, fields = load_csv(CANONICAL_FILE)
    by_date = {}
    for row in canonical_rows:
        date_str = (row.get("_Source_Date") or "").strip()
        domain = normalize_domain(row.get("Domain", ""))
        if date_str and domain:
            by_date.setdefault(date_str, {})[domain] = row

    if not os.path.isdir(PROCESSING_QUEUE_DIR):
        return 0

    for date_str in sorted(os.listdir(PROCESSING_QUEUE_DIR)):
        workspace = os.path.join(PROCESSING_QUEUE_DIR, date_str)
        source = os.path.join(workspace, "Ultimate_God_Leads.csv")
        done = os.path.join(workspace, "filter_2.done")
        archive = os.path.join(master_dir, f"Final_Extracted_Leads_{date_str}.csv")
        if not (os.path.exists(done) and os.path.exists(source)):
            continue
        if os.path.exists(archive):
            continue
        source_rows, _ = load_csv(source)
        source_domains = {normalize_domain(r.get("Domain", "")) for r in source_rows if normalize_domain(r.get("Domain", ""))}
        classified = by_date.get(date_str, {})
        if source_domains and not source_domains.issubset(set(classified)):
            continue
        archive_fields = [f for f in fields if f not in {"_Source_Date", "_Worker", "_Processed_At"}]
        out_rows = list(classified.values())
        write_csv_atomic(archive, out_rows, archive_fields)
        archived += 1
    return archived


def publish_result_checkpoint(claim, produced_csv):
    """Publish a partial/complete result shard without releasing claims."""
    if not os.path.exists(produced_csv):
        return 0
    rows, fields = load_csv(produced_csv)
    if not rows:
        return 0
    write_csv_atomic(claim["result_file"], rows, fields)
    rel = os.path.relpath(claim["result_file"], BASE_DIR)
    git("add", rel, check=True, capture=True)
    git("commit", "-m", f"💾 AI checkpoint {claim['worker']} {claim['claim_id']}", check=True, capture=True)
    git_push_with_rebase(f"AI checkpoint {claim['worker']} {claim['claim_id']}")
    return len(rows)
