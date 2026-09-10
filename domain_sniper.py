import os
import sys
import time
import zipfile
import shutil
import re
from datetime import datetime
from urllib.parse import urljoin

import cloudscraper
from bs4 import BeautifulSoup


# ============================================================
# BAWA DOMAIN SNIPER v2.2 (FIXED)
# ------------------------------------------------------------
# Features:
#   ✅ Historical missing-date recovery
#   ✅ Targeted date mode for master_controller.py
#   ✅ Standalone full-history mode
#   ✅ Cloudflare-compatible scraper
#   ✅ Explicit redirect handling
#   ✅ Retry + exponential backoff
#   ✅ HTML/error-page detection
#   ✅ ZIP integrity validation
#   ✅ Safe ZIP extraction (Zip Slip protection)
#   ✅ Atomic final file replacement
#   ✅ Zero-byte / broken-file protection
#   ✅ Temp cleanup
#
# v2.2 FIX (over v2.1):
#   process_date() used to build/clean its temp workspace
#   (safe_remove / rmtree / os.makedirs) OUTSIDE the try block.
#   If that raised an OSError (disk full, permissions, weird
#   filesystem state), the exception would bubble all the way
#   up to sync_historical_whois_data()'s outer try/except and
#   get logged as a FATAL error — aborting the ENTIRE run and
#   skipping every remaining date, not just the bad one. Now
#   that setup/cleanup is inside process_date's own try/except,
#   so a single date's filesystem hiccup only fails that date.
# ============================================================


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SAVE_FOLDER = os.path.join(
    BASE_DIR,
    "daily_domains"
)

WHOISDS_URL = (
    "https://www.whoisds.com/"
    "newly-registered-domains"
)

REQUEST_TIMEOUT = (15, 60)

MAX_PAGE_RETRIES = 4
MAX_DOWNLOAD_RETRIES = 4

BASE_RETRY_SLEEP = 3
MAX_RETRY_SLEEP = 30

CHUNK_SIZE = 1024 * 1024  # 1 MB

DATE_PATTERN = re.compile(
    r"\b(\d{4}-\d{2}-\d{2})\b"
)


# ============================================================
# LOGGING
# ============================================================

def log(message):
    timestamp = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    print(
        f"[{timestamp}] {message}",
        flush=True
    )


# ============================================================
# FILE HELPERS
# ============================================================

def ensure_storage():
    os.makedirs(
        SAVE_FOLDER,
        exist_ok=True
    )


def expected_file_for_date(date_str):
    filename = (
        f"Whois_Leads_Extracted_{date_str}.txt"
    )

    return os.path.join(
        SAVE_FOLDER,
        filename
    )


def is_file_healthy(filepath):
    """
    Existing file ko valid tabhi maana jaayega jab:
        - file exist karti ho
        - regular file ho
        - size > 0 ho
    """

    if not os.path.isfile(filepath):
        return False

    try:
        return os.path.getsize(filepath) > 0
    except OSError:
        return False


def safe_remove(filepath):
    if not filepath:
        return

    try:
        if os.path.exists(filepath):
            os.remove(filepath)
    except OSError as exc:
        log(
            f"[!] Could not remove file "
            f"{filepath}: {exc}"
        )


# ============================================================
# SCRAPER
# ============================================================

def create_scraper():
    return cloudscraper.create_scraper(
        browser={
            "browser": "chrome",
            "platform": "windows",
            "desktop": True,
        }
    )


# ============================================================
# RETRY
# ============================================================

def retry_sleep(attempt):
    delay = min(
        BASE_RETRY_SLEEP * (2 ** (attempt - 1)),
        MAX_RETRY_SLEEP,
    )

    log(
        f"[~] Waiting {delay}s before retry..."
    )

    time.sleep(delay)


# ============================================================
# WHOISDS HISTORY PAGE
# ============================================================

def fetch_history_page(scraper):

    last_error = None

    for attempt in range(
        1,
        MAX_PAGE_RETRIES + 1
    ):

        try:

            log(
                f"[+] Accessing WhoisDS history "
                f"(attempt {attempt}/{MAX_PAGE_RETRIES})..."
            )

            response = scraper.get(
                WHOISDS_URL,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )

            status = response.status_code

            if status == 200:
                return response

            last_error = (
                f"HTTP {status}"
            )

            log(
                f"[!] WhoisDS returned HTTP {status}"
            )

            if status in {
                408,
                425,
                429,
                500,
                502,
                503,
                504,
            } and attempt < MAX_PAGE_RETRIES:

                retry_sleep(attempt)
                continue

        except Exception as exc:

            last_error = str(exc)

            log(
                f"[!] WhoisDS request failed: {exc}"
            )

            if attempt < MAX_PAGE_RETRIES:
                retry_sleep(attempt)

    raise RuntimeError(
        "Could not fetch WhoisDS history page. "
        f"Last error: {last_error}"
    )


# ============================================================
# PARSE HISTORY TABLE
# ============================================================

def parse_history_table(html):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    table = soup.find("table")

    if not table:
        raise RuntimeError(
            "WhoisDS history table nahi mili. "
            "Possible website/DOM change."
        )

    results = []

    for row in table.find_all("tr"):

        cols = row.find_all("td")

        if len(cols) < 3:
            continue

        row_text = row.get_text(
            " ",
            strip=True
        )

        date_match = DATE_PATTERN.search(
            row_text
        )

        if not date_match:
            continue

        date_str = date_match.group(1)

        # Validate actual date
        try:
            datetime.strptime(
                date_str,
                "%Y-%m-%d"
            )
        except ValueError:
            continue

        link_tag = row.find(
            "a",
            href=True
        )

        if not link_tag:
            continue

        href = (
            link_tag.get("href", "")
            .strip()
        )

        if not href:
            continue

        href_lower = href.lower()

        # Unrelated links skip
        excluded_terms = (
            "whois-database-download",
            "contact",
        )

        if any(
            term in href_lower
            for term in excluded_terms
        ):
            continue

        download_url = urljoin(
            "https://www.whoisds.com/",
            href
        )

        results.append(
            {
                "date": date_str,
                "url": download_url,
            }
        )

    # De-duplicate by date
    unique = {}

    for item in results:
        if item["date"] not in unique:
            unique[item["date"]] = item["url"]

    return [
        {
            "date": date_str,
            "url": download_url,
        }
        for date_str, download_url in sorted(
            unique.items()
        )
    ]


# ============================================================
# DOWNLOAD VALIDATION
# ============================================================

def looks_like_html(response):

    content_type = (
        response.headers
        .get("Content-Type", "")
        .lower()
    )

    if "text/html" in content_type:
        return True

    # Some servers send ZIP with wrong/missing content-type,
    # so inspect only a tiny prefix.
    try:
        prefix = (
            response.content[:200]
            .lstrip()
            .lower()
        )

        if (
            prefix.startswith(
                b"<!doctype html"
            )
            or prefix.startswith(
                b"<html"
            )
            or prefix.startswith(
                b"<head"
            )
        ):
            return True

    except Exception:
        pass

    return False


# ============================================================
# DOWNLOAD ZIP
# ============================================================

def download_zip(
    scraper,
    download_url,
    destination,
):

    last_error = None

    for attempt in range(
        1,
        MAX_DOWNLOAD_RETRIES + 1
    ):

        safe_remove(destination)

        try:

            log(
                f"[+] Download attempt "
                f"{attempt}/{MAX_DOWNLOAD_RETRIES}"
            )

            response = scraper.get(
                download_url,
                stream=True,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )

            status = response.status_code

            if status != 200:

                last_error = (
                    f"HTTP {status}"
                )

                log(
                    f"[!] Download failed: "
                    f"HTTP {status}"
                )

                if status in {
                    408,
                    425,
                    429,
                    500,
                    502,
                    503,
                    504,
                } and attempt < MAX_DOWNLOAD_RETRIES:

                    retry_sleep(attempt)
                    continue

                return False

            # ------------------------------------------------
            # HTML / Cloudflare / error response protection
            # ------------------------------------------------

            if looks_like_html(response):

                last_error = (
                    "HTML response instead of ZIP"
                )

                log(
                    "[!] Server returned HTML "
                    "instead of ZIP."
                )

                if attempt < MAX_DOWNLOAD_RETRIES:
                    retry_sleep(attempt)
                    continue

                return False

            # ------------------------------------------------
            # Stream download
            # ------------------------------------------------

            with open(
                destination,
                "wb"
            ) as output_file:

                for chunk in response.iter_content(
                    chunk_size=CHUNK_SIZE
                ):

                    if chunk:
                        output_file.write(chunk)

            # ------------------------------------------------
            # File check
            # ------------------------------------------------

            if not is_file_healthy(
                destination
            ):

                last_error = (
                    "Downloaded file is empty"
                )

                log(
                    "[!] Downloaded file is empty."
                )

                if attempt < MAX_DOWNLOAD_RETRIES:
                    retry_sleep(attempt)
                    continue

                return False

            # ------------------------------------------------
            # ZIP check
            # ------------------------------------------------

            if not zipfile.is_zipfile(
                destination
            ):

                last_error = (
                    "Downloaded file is not valid ZIP"
                )

                log(
                    "[!] Downloaded file is not "
                    "a valid ZIP archive."
                )

                if attempt < MAX_DOWNLOAD_RETRIES:
                    retry_sleep(attempt)
                    continue

                return False

            log(
                "[+] ZIP download validated successfully."
            )

            return True

        except Exception as exc:

            last_error = str(exc)

            log(
                f"[!] Download error: {exc}"
            )

            if attempt < MAX_DOWNLOAD_RETRIES:
                retry_sleep(attempt)

    log(
        f"[-] Download permanently failed: "
        f"{last_error}"
    )

    return False


# ============================================================
# SAFE ZIP EXTRACTION
# ============================================================

def safe_extract_first_data_file(
    zip_path,
    extract_dir,
):

    try:

        with zipfile.ZipFile(
            zip_path,
            "r"
        ) as zip_ref:

            members = [
                member
                for member in zip_ref.infolist()
                if not member.is_dir()
            ]

            if not members:

                log(
                    "[-] ZIP mein koi actual file nahi mili."
                )

                return None

            # Prefer TXT/CSV/LIST files
            preferred = [
                member
                for member in members
                if member.filename.lower().endswith(
                    (
                        ".txt",
                        ".csv",
                        ".list",
                    )
                )
            ]

            selected = (
                preferred[0]
                if preferred
                else members[0]
            )

            # ------------------------------------------------
            # Zip Slip protection
            # ------------------------------------------------

            extract_root = os.path.abspath(
                extract_dir
            )

            target_path = os.path.abspath(
                os.path.join(
                    extract_dir,
                    selected.filename
                )
            )

            if not (
                target_path == extract_root
                or target_path.startswith(
                    extract_root + os.sep
                )
            ):

                log(
                    f"[-] BLOCKED unsafe ZIP path: "
                    f"{selected.filename}"
                )

                return None

            os.makedirs(
                os.path.dirname(target_path),
                exist_ok=True
            )

            # ------------------------------------------------
            # Controlled extraction
            # ------------------------------------------------

            with zip_ref.open(
                selected,
                "r"
            ) as source, open(
                target_path,
                "wb"
            ) as destination:

                shutil.copyfileobj(
                    source,
                    destination,
                    length=CHUNK_SIZE
                )

            if not is_file_healthy(
                target_path
            ):

                log(
                    "[-] Extracted file is empty."
                )

                safe_remove(target_path)

                return None

            return target_path

    except zipfile.BadZipFile:

        log(
            "[-] BadZipFile: archive corrupt hai."
        )

        return None

    except Exception as exc:

        log(
            f"[-] ZIP extraction failed: {exc}"
        )

        return None


# ============================================================
# ATOMIC FINALIZATION
# ============================================================

def finalize_extracted_file(
    extracted_file,
    expected_filepath,
):

    temp_final = (
        expected_filepath
        + ".writing"
    )

    try:

        safe_remove(temp_final)

        shutil.copy2(
            extracted_file,
            temp_final
        )

        if not is_file_healthy(
            temp_final
        ):

            log(
                "[-] Temporary final file invalid."
            )

            return False

        # Atomic replace
        os.replace(
            temp_final,
            expected_filepath
        )

        return is_file_healthy(
            expected_filepath
        )

    except Exception as exc:

        log(
            f"[-] Finalize failed: {exc}"
        )

        safe_remove(
            temp_final
        )

        return False


# ============================================================
# PROCESS SINGLE DATE
# ============================================================

def process_date(
    scraper,
    date_str,
    download_url,
):

    expected_filepath = (
        expected_file_for_date(
            date_str
        )
    )

    # Existing valid file = no work needed
    if is_file_healthy(
        expected_filepath
    ):

        log(
            f"[~] Safe: {date_str} "
            f"already available."
        )

        return True

    log("")
    log(
        f"[!] Missing Data Detected: {date_str}"
    )
    log(
        f"[+] Target Locked: {download_url}"
    )

    zip_file_path = os.path.join(
        SAVE_FOLDER,
        f".temp_whois_{date_str}.zip"
    )

    extract_workspace = os.path.join(
        SAVE_FOLDER,
        f".extract_{date_str}"
    )

    # ----------------------------------------------------------
    # v2.2 FIX: everything from workspace prep to cleanup is now
    # inside ONE try/except OSError. Previously, safe_remove()/
    # rmtree()/os.makedirs() ran outside any try block here — an
    # OSError from any of them (disk full, permission denied,
    # weird filesystem state) would propagate all the way up and
    # be treated as a FATAL error by the caller, aborting the
    # entire sync run (all remaining dates too). Now a filesystem
    # problem on one date only fails that one date.
    # ----------------------------------------------------------

    try:

        # Clean stale temporary artifacts
        safe_remove(
            zip_file_path
        )

        if os.path.exists(
            extract_workspace
        ):
            shutil.rmtree(
                extract_workspace,
                ignore_errors=True
            )

        os.makedirs(
            extract_workspace,
            exist_ok=True
        )

        try:

            # ----------------------------------------------------
            # Download
            # ----------------------------------------------------

            if not download_zip(
                scraper,
                download_url,
                zip_file_path,
            ):

                log(
                    f"[-] {date_str}: download failed."
                )

                return False

            # ----------------------------------------------------
            # Extract
            # ----------------------------------------------------

            log(
                f"[+] Extracting {date_str}..."
            )

            extracted_file = (
                safe_extract_first_data_file(
                    zip_file_path,
                    extract_workspace,
                )
            )

            if not extracted_file:

                log(
                    f"[-] {date_str}: extraction failed."
                )

                return False

            # ----------------------------------------------------
            # Finalize
            # ----------------------------------------------------

            if finalize_extracted_file(
                extracted_file,
                expected_filepath,
            ):

                log(
                    f"[+] SUCCESS: "
                    f"{os.path.basename(expected_filepath)} "
                    f"saved ✅"
                )

                return True

            log(
                f"[-] {date_str}: finalization failed."
            )

            return False

        finally:

            safe_remove(
                zip_file_path
            )

            if os.path.exists(
                extract_workspace
            ):
                shutil.rmtree(
                    extract_workspace,
                    ignore_errors=True
                )

    except OSError as exc:

        log(
            f"[-] {date_str}: filesystem error while "
            f"preparing/cleaning workspace: {exc}"
        )

        return False


# ============================================================
# MAIN SYNC FUNCTION
# ============================================================

def sync_historical_whois_data(
    target_date_str=None
):
    """
    Two modes:

    1. target_date_str provided:
       Only that exact date is processed.
       Used by master_controller.py.

    2. target_date_str=None:
       Entire visible WhoisDS history is checked.
       Useful for standalone recovery/manual execution.
    """

    log("")
    log(
        "[+] SYSTEM WAKING UP..."
    )

    if target_date_str:
        log(
            f"[+] Targeted mode: "
            f"Checking {target_date_str}"
        )
    else:
        log(
            "[+] Full-history mode: "
            "Scanning all missing dates."
        )

    ensure_storage()

    scraper = create_scraper()

    try:

        # ====================================================
        # FETCH HISTORY
        # ====================================================

        response = fetch_history_page(
            scraper
        )

        # ====================================================
        # PARSE
        # ====================================================

        try:

            history_items = parse_history_table(
                response.text
            )

        except Exception as exc:

            log(
                f"[-] Could not parse WhoisDS history: "
                f"{exc}"
            )

            return False

        if not history_items:

            log(
                "[-] History table mili, "
                "lekin valid entries nahi mile."
            )

            return False

        log(
            f"[+] Found {len(history_items)} "
            f"historical entries."
        )

        # ====================================================
        # TARGETED MODE
        # ====================================================

        if target_date_str:

            # Basic target date validation
            try:
                datetime.strptime(
                    target_date_str,
                    "%Y-%m-%d"
                )
            except ValueError:

                log(
                    f"[-] Invalid target date: "
                    f"{target_date_str}"
                )

                return False

            target_item = next(
                (
                    item
                    for item in history_items
                    if item["date"] == target_date_str
                ),
                None
            )

            expected_filepath = (
                expected_file_for_date(
                    target_date_str
                )
            )

            # Already downloaded locally
            if is_file_healthy(
                expected_filepath
            ):

                log(
                    f"[+] Target {target_date_str} "
                    f"already exists locally."
                )

                return True

            # Date not yet visible on WhoisDS
            if not target_item:

                log(
                    f"[-] Target date {target_date_str} "
                    f"is NOT available on WhoisDS."
                )

                return False

            log(
                f"[+] Target date {target_date_str} "
                f"found on WhoisDS."
            )

            success = process_date(
                scraper,
                target_item["date"],
                target_item["url"],
            )

            if success:

                log(
                    f"[+] Targeted sync complete: "
                    f"{target_date_str}"
                )

                return True

            log(
                f"[-] Targeted sync failed: "
                f"{target_date_str}"
            )

            return False

        # ====================================================
        # FULL HISTORY MODE
        # ====================================================

        downloaded_count = 0
        already_present = 0
        failed_count = 0

        for item in history_items:

            date_str = item["date"]
            download_url = item["url"]

            expected_filepath = (
                expected_file_for_date(
                    date_str
                )
            )

            if is_file_healthy(
                expected_filepath
            ):

                already_present += 1

                log(
                    f"[~] Already present: "
                    f"{date_str}"
                )

                continue

            if process_date(
                scraper,
                date_str,
                download_url,
            ):

                downloaded_count += 1

            else:

                failed_count += 1

        # ====================================================
        # SUMMARY
        # ====================================================

        log("")
        log("=" * 65)
        log("WHOISDS FULL SYNC COMPLETE")
        log("=" * 65)

        log(
            f"📅 History entries found : "
            f"{len(history_items)}"
        )

        log(
            f"✅ Already present       : "
            f"{already_present}"
        )

        log(
            f"📥 Newly downloaded      : "
            f"{downloaded_count}"
        )

        log(
            f"❌ Failed                : "
            f"{failed_count}"
        )

        log(
            f"📁 Storage               : "
            f"{SAVE_FOLDER}"
        )

        log("=" * 65)

        return True

    except Exception as exc:

        log(
            f"[-] FATAL SNIPER ERROR: "
            f"{exc}"
        )

        return False


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    start_time = time.perf_counter()

    log(
        "[+] Domain Sniper starting..."
    )

    # --------------------------------------------------------
    # Controller compatibility:
    #
    # python domain_sniper.py 2026-07-30
    #
    # Standalone:
    #
    # python domain_sniper.py
    # --------------------------------------------------------

    requested_date = (
        sys.argv[1].strip()
        if len(sys.argv) > 1
        else None
    )

    success = sync_historical_whois_data(
        requested_date
    )

    elapsed = (
        time.perf_counter()
        - start_time
    )

    log(
        f"[+] Sniper finished in "
        f"{elapsed:.2f}s"
    )

    if not success:
        raise SystemExit(1)

    raise SystemExit(0)
