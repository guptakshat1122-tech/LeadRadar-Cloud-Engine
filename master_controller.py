import os
import sys
import time
import subprocess
import socket
import shutil
import re
from datetime import datetime

# ==========================================
# 📁 REPO-RELATIVE PATHS (Linux/GitHub Actions safe)
# Sab kuch is script ke folder ke andar hi rehta hai —
# koi hardcoded Windows path nahi.
# ==========================================
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
RAW_DATA_DIR = os.path.join(BASE_DIR, "daily_domains")        # sniper ka raw download output
STATE_DIR    = os.path.join(BASE_DIR, "state")                # filter/scanner/categorizer working files
MASTER_DIR   = os.path.join(BASE_DIR, "master_control_room")  # final archived CSVs

SNIPER_SCRIPT   = os.path.join(BASE_DIR, "domain_sniper.py")
FILTER_1_SCRIPT = os.path.join(BASE_DIR, "1_domain_filter.py")
FILTER_2_SCRIPT = os.path.join(BASE_DIR, "2_deep_xray_scanner.py")
FILTER_3_SCRIPT = os.path.join(BASE_DIR, "3_lead_categorizer.py")

PYTHON = sys.executable  # "python" GitHub runner pe exist nahi karta — sys.executable hamesha sahi hota hai

# GitHub Actions job ka default max 6 ghanta hota hai — usse pehle hi safely exit kar jaayein
MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", 5 * 3600 + 30 * 60))  # 5.5 hr default

for d in (RAW_DATA_DIR, STATE_DIR, MASTER_DIR):
    os.makedirs(d, exist_ok=True)

# ==========================================
# 🔧 HELPER FUNCTIONS
# ==========================================
def check_internet():
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3)
        return True
    except OSError:
        return False

def is_workspace_dirty():
    files_to_check = ["domain-names.txt", "premium_domains.txt", "Ultimate_God_Leads.csv", "scanned_cache.txt"]
    return any(os.path.exists(os.path.join(STATE_DIR, f)) for f in files_to_check)

def find_raw_file(target_date_str):
    if not os.path.exists(RAW_DATA_DIR):
        return None
    for f in os.listdir(RAW_DATA_DIR):
        if target_date_str in f and f.lower().endswith(".txt"):
            return os.path.join(RAW_DATA_DIR, f)
    return None

def extract_date_from_filename(filename):
    match = re.search(r'\d{4}-\d{2}-\d{2}', filename)
    return match.group(0) if match else None

def get_all_raw_data_dates():
    dates_found = []
    if os.path.exists(RAW_DATA_DIR):
        for f in os.listdir(RAW_DATA_DIR):
            if f.lower().endswith(".txt"):
                date_str = extract_date_from_filename(f)
                if date_str:
                    try:
                        dates_found.append(datetime.strptime(date_str, "%Y-%m-%d").date())
                    except ValueError:
                        pass
    return sorted(set(dates_found))

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def run_step(script_path, timeout=None):
    """Har filter/scanner/categorizer script ko STATE_DIR ke andar chalao."""
    return subprocess.run([PYTHON, script_path], check=True, cwd=STATE_DIR, timeout=timeout)

# ==========================================
# ✅ STEP COMPLETION CHECKER
# ==========================================
def get_step_status():
    return {
        "raw_file":   os.path.exists(os.path.join(STATE_DIR, "domain-names.txt")),
        "step1_done": os.path.exists(os.path.join(STATE_DIR, "premium_domains.txt")),
        "step2_done": os.path.exists(os.path.join(STATE_DIR, "filter_2.done")),
        "step3_done": os.path.exists(os.path.join(STATE_DIR, "Bawa_Categorized_Leads.csv")),
        "has_cache":  os.path.exists(os.path.join(STATE_DIR, "scanned_cache.txt")),
        "has_god":    os.path.exists(os.path.join(STATE_DIR, "Ultimate_God_Leads.csv")),
    }

# ==========================================
# 🚀 PIPELINE FOR ONE DATE
# ==========================================
def fire_the_pipeline(target_date_str, is_resume=False):
    log(f"[!!!] STARTING PIPELINE FOR DATE: {target_date_str} [!!!]")

    date_lock_file = os.path.join(STATE_DIR, "date_lock.txt")
    with open(date_lock_file, "w") as f:
        f.write(target_date_str)

    # ---------------- STEP 1: SNIPER (full historical sync, like original PC design) ----------------
    if not is_resume:
        log("STEP 1: Firing Sniper (checking ALL missing historical dates on WhoisDS)...")
        try:
            subprocess.run([PYTHON, SNIPER_SCRIPT], check=True, cwd=BASE_DIR, timeout=1800)
        except subprocess.TimeoutExpired:
            log("Sniper timed out.")
            return "ERROR"
        except Exception as e:
            log(f"Sniper failed: {e}")
            return "ERROR"

        downloaded_file = find_raw_file(target_date_str)
        target_input    = os.path.join(STATE_DIR, "domain-names.txt")

        if downloaded_file and os.path.exists(downloaded_file):
            shutil.copy(downloaded_file, target_input)
            log(f"Raw data piped for {target_date_str}.")
        else:
            return "MISSING"
    else:
        log("SKIPPING Sniper — resuming existing backlog.")

    status = get_step_status()

    # ---------------- STEP 2: DOMAIN FILTER (Level 1) ----------------
    if not status["step1_done"]:
        log("STEP 2: Running Level 1 Domain Filter...")
        try:
            run_step(FILTER_1_SCRIPT, timeout=600)
        except Exception as e:
            log(f"Filter 1 failed: {e}")
            return "ERROR"
    else:
        log("STEP 2: Already done — skipping.")

    # ---------------- STEP 3: X-RAY SCANNER (Level 2) ----------------
    if not status["step2_done"]:
        log("STEP 3: Running X-Ray Scanner (Level 2)...")
        MAX_SCANNER_RETRIES = 5
        scanner_attempts    = 0

        while scanner_attempts < MAX_SCANNER_RETRIES:
            if not check_internet():
                log("No internet — waiting 30s...")
                time.sleep(30)
                continue

            scanner_attempts += 1
            log(f"Scanner attempt {scanner_attempts}/{MAX_SCANNER_RETRIES}...")

            try:
                run_step(FILTER_2_SCRIPT, timeout=7200)
            except subprocess.TimeoutExpired:
                log("Scanner timed out — will retry (scanned_cache.txt se resume hoga).")
                continue
            except Exception as e:
                log(f"Scanner error: {e}")
                time.sleep(10)
                continue

            if os.path.exists(os.path.join(STATE_DIR, "filter_2.done")):
                log("Scanner completed successfully!")
                break

            god_csv = os.path.join(STATE_DIR, "Ultimate_God_Leads.csv")
            if os.path.exists(god_csv) and os.path.getsize(god_csv) > 1000:
                log("God Leads file found — marking scanner as done.")
                open(os.path.join(STATE_DIR, "filter_2.done"), 'w').close()
                break

            log(f"Scanner incomplete — will retry ({scanner_attempts}/{MAX_SCANNER_RETRIES})...")
            time.sleep(15)

        if not os.path.exists(os.path.join(STATE_DIR, "filter_2.done")):
            log("❌ Scanner failed after all retries. Skipping this date.")
            return "ERROR"
    else:
        log("STEP 3: Already done — skipping.")

    # ---------------- STEP 4: AI CATEGORIZER (Level 3, Groq) ----------------
    if not os.path.exists(os.path.join(STATE_DIR, "Bawa_Categorized_Leads.csv")):
        log("STEP 4: Running AI Categorizer (Groq)...")
        try:
            run_step(FILTER_3_SCRIPT, timeout=14400)
        except subprocess.TimeoutExpired:
            log("Categorizer timed out — partial output save ho gaya hoga.")
        except Exception as e:
            log(f"Categorizer error: {e}")
            return "ERROR"
    else:
        log("STEP 4: Already done — skipping.")

    # ---------------- STEP 5: CLEANUP & ARCHIVE ----------------
    final_temp   = os.path.join(STATE_DIR, "Bawa_Categorized_Leads.csv")
    archived_out = os.path.join(MASTER_DIR, f"Final_Extracted_Leads_{target_date_str}.csv")

    if os.path.exists(final_temp):
        shutil.move(final_temp, archived_out)
        log(f"🎉 DONE! VIP List saved: {archived_out}")

        for temp_file in ["domain-names.txt", "premium_domains.txt", "Ultimate_God_Leads.csv",
                           "scanned_cache.txt", "filter_2.done", "date_lock.txt"]:
            tf = os.path.join(STATE_DIR, temp_file)
            if os.path.exists(tf):
                os.remove(tf)

        log(f"Workspace clean. {target_date_str} closed ✅")
        return "SUCCESS"

    log("❌ Final output file missing — something went wrong.")
    return "ERROR"

# ==========================================
# 🔄 SINGLE-RUN AUTOMATION (GitHub Actions safe)
# Infinite while-loop hata diya gaya hai — ab ye function
# ek bounded time-budget ke andar jitni dates process kar sakta
# hai karta hai, phir cleanly exit karta hai. Cron schedule
# agle din firse trigger karega.
# ==========================================
def main():
    log("Master Controller ONLINE (single-run mode).")
    run_start = time.time()

    while True:
        elapsed = time.time() - run_start
        if elapsed > MAX_RUNTIME_SECONDS:
            log("⏱️ Time budget khatam — cleanly exiting is run se. Agla scheduled run baaki kaam karega.")
            break

        if not check_internet():
            log("No internet available right now — exiting this run.")
            break

        # Scenario 1: Resume incomplete work
        if is_workspace_dirty():
            date_lock_file = os.path.join(STATE_DIR, "date_lock.txt")
            if os.path.exists(date_lock_file):
                with open(date_lock_file, "r") as f:
                    locked_date = f.read().strip()
                log(f"🛑 BACKLOG: Resuming incomplete work for {locked_date}...")
                log(f"   Current status: {get_step_status()}")
                fire_the_pipeline(locked_date, is_resume=True)
                continue
            else:
                log("⚠️  Dirty workspace but no lock file — cleaning up...")
                for temp_file in ["domain-names.txt", "premium_domains.txt",
                                   "scanned_cache.txt", "filter_2.done"]:
                    tf = os.path.join(STATE_DIR, temp_file)
                    if os.path.exists(tf):
                        os.remove(tf)
                        log(f"   Removed: {temp_file}")
                continue

        # Scenario 2: Process pending backlog dates
        log("Scanning all raw data dates...")
        all_dates          = get_all_raw_data_dates()
        retro_action_taken = False

        for retro_date in all_dates:
            retro_str      = retro_date.strftime("%Y-%m-%d")
            final_csv_path = os.path.join(MASTER_DIR, f"Final_Extracted_Leads_{retro_str}.csv")

            needs_processing = False
            if not os.path.exists(final_csv_path):
                needs_processing = True
            else:
                try:
                    with open(final_csv_path, 'r', encoding='utf-8') as f:
                        content = f.read(200)
                        if "NO_DATA" in content or len(content) < 50:
                            needs_processing = True
                except Exception:
                    needs_processing = True

            if needs_processing:
                log(f"🎯 Processing backlog date: {retro_str}")
                if os.path.exists(final_csv_path):
                    os.remove(final_csv_path)
                fire_the_pipeline(retro_str, is_resume=False)
                retro_action_taken = True
                break

        if retro_action_taken:
            continue

        # Scenario 3: Today's data
        today_str      = datetime.now().date().strftime("%Y-%m-%d")
        final_csv_path = os.path.join(MASTER_DIR, f"Final_Extracted_Leads_{today_str}.csv")

        if not os.path.exists(final_csv_path):
            log(f"Fetching today's data: {today_str}...")
            status = fire_the_pipeline(today_str, is_resume=False)
            if status == "MISSING":
                log(f"Aaj ({today_str}) ka data abhi WhoisDS pe nahi hai — is run me kuch nahi ho sakta.")
                lf = os.path.join(STATE_DIR, "date_lock.txt")
                if os.path.exists(lf):
                    os.remove(lf)
            # Chahe SUCCESS ho, ERROR ho, ya MISSING — is run me aur kuch process nahi karna
            break
        else:
            log("✅ Aaj ka data already ban chuka hai. Kuch bhi pending nahi. Exiting.")
            break

    log("Master Controller run complete. Exiting.")

if __name__ == "__main__":
    main()
