import os
import time
import subprocess
import socket
import shutil
import re
from datetime import datetime, timedelta

# ==========================================
# 🛑 BAWA'S EXACT DIRECTORY PATHS
# ==========================================
BASE_DIR     = r"C:\Users\aksha\Documents\lead generation"
RAW_DATA_DIR = os.path.join(BASE_DIR, "daily_domains")
SNIPER_DIR   = os.path.join(BASE_DIR, "domains raw data")
FILTER_DIR   = os.path.join(BASE_DIR, "leads from daily domain list")
MASTER_DIR   = os.path.join(BASE_DIR, "master_control_room")

SNIPER_SCRIPT   = os.path.join(SNIPER_DIR, "domain_sniper.py")
FILTER_1_SCRIPT = os.path.join(FILTER_DIR, "1_domain_filter.py")
FILTER_2_SCRIPT = os.path.join(FILTER_DIR, "2_deep_xray_scanner.py")
FILTER_3_SCRIPT = os.path.join(FILTER_DIR, "3_lead_categorizer.py")

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
    return any(os.path.exists(os.path.join(FILTER_DIR, f)) for f in files_to_check)

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
    return sorted(list(set(dates_found)))

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

# ==========================================
# ✅ STEP COMPLETION CHECKER
# Har step ka progress track karo separately
# ==========================================
def get_step_status():
    """Abhi pipeline kis step pe hai — check karo."""
    f1_done = os.path.exists(os.path.join(FILTER_DIR, "premium_domains.txt"))
    f2_done = os.path.exists(os.path.join(FILTER_DIR, "filter_2.done"))
    f3_done = os.path.exists(os.path.join(FILTER_DIR, "Bawa_Categorized_Leads.csv"))
    raw     = os.path.exists(os.path.join(FILTER_DIR, "domain-names.txt"))
    cache   = os.path.exists(os.path.join(FILTER_DIR, "scanned_cache.txt"))
    god     = os.path.exists(os.path.join(FILTER_DIR, "Ultimate_God_Leads.csv"))

    return {
        "raw_file":   raw,
        "step1_done": f1_done,
        "step2_done": f2_done,
        "step3_done": f3_done,
        "has_cache":  cache,
        "has_god":    god,
    }

# ==========================================
# 🚀 MAIN PIPELINE
# ==========================================
def fire_the_pipeline(target_date_str, is_resume=False):
    log(f"[!!!] STARTING PIPELINE FOR DATE: {target_date_str} [!!!]")

    if not os.path.exists(MASTER_DIR):
        os.makedirs(MASTER_DIR)

    # Lock file — remember which date we are working on
    date_lock_file = os.path.join(FILTER_DIR, "date_lock.txt")
    with open(date_lock_file, "w") as f:
        f.write(target_date_str)

    status = get_step_status()

    # ---------------------------------------------------------
    # STEP 1: SNIPER — Raw domain file download
    # ---------------------------------------------------------
    if not is_resume:
        log("STEP 1: Firing Sniper...")
        try:
            subprocess.run(["python", SNIPER_SCRIPT, target_date_str], check=True, cwd=SNIPER_DIR)
        except subprocess.CalledProcessError:
            return "MISSING"
        except Exception as e:
            log(f"Sniper failed: {e}")
            return "ERROR"

        downloaded_file = find_raw_file(target_date_str)
        target_input    = os.path.join(FILTER_DIR, "domain-names.txt")

        if downloaded_file and os.path.exists(downloaded_file):
            shutil.copy(downloaded_file, target_input)
            log(f"Raw data piped for {target_date_str}.")
        else:
            return "MISSING"
    else:
        log("SKIPPING Sniper — resuming existing backlog.")

    # Re-check status after sniper
    status = get_step_status()

    # ---------------------------------------------------------
    # STEP 2: DOMAIN FILTER (Level 1)
    # ---------------------------------------------------------
    if not status["step1_done"]:
        log("STEP 2: Running Level 1 Domain Filter...")
        try:
            subprocess.run(["python", FILTER_1_SCRIPT], check=True, cwd=FILTER_DIR)
        except Exception as e:
            log(f"Filter 1 failed: {e}")
            return "ERROR"
    else:
        log("STEP 2: Already done — skipping.")

    # ---------------------------------------------------------
    # STEP 3: X-RAY SCANNER (Level 2)
    # FIX: Agar scanner crash kare toh infinite loop nahi
    # Scanner khud resume karta hai scanned_cache.txt se
    # ---------------------------------------------------------
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
                subprocess.run(["python", FILTER_2_SCRIPT], cwd=FILTER_DIR, timeout=7200)  # 2hr max
            except subprocess.TimeoutExpired:
                log("Scanner timed out — will retry (scanned_cache.txt se resume hoga).")
                continue
            except Exception as e:
                log(f"Scanner error: {e}")
                time.sleep(10)
                continue

            # Check karo scanner ne kaam kiya ya nahi
            if os.path.exists(os.path.join(FILTER_DIR, "filter_2.done")):
                log("Scanner completed successfully!")
                break

            # ✅ FIX: Agar filter_2.done nahi bana but God Leads file hai
            # toh manually done mark karo
            if os.path.exists(os.path.join(FILTER_DIR, "Ultimate_God_Leads.csv")):
                god_size = os.path.getsize(os.path.join(FILTER_DIR, "Ultimate_God_Leads.csv"))
                if god_size > 1000:  # File mein actual data hai
                    log("God Leads file found — marking scanner as done.")
                    open(os.path.join(FILTER_DIR, "filter_2.done"), 'w').close()
                    break

            log(f"Scanner incomplete — will retry ({scanner_attempts}/{MAX_SCANNER_RETRIES})...")
            time.sleep(15)

        if not os.path.exists(os.path.join(FILTER_DIR, "filter_2.done")):
            log("❌ Scanner failed after all retries. Skipping this date.")
            return "ERROR"
    else:
        log("STEP 3: Already done — skipping.")

    # ---------------------------------------------------------
    # STEP 4: AI CATEGORIZER (Level 3)
    # FIX: Categorizer khud resume karta hai — sirf ek baar call karo
    # ---------------------------------------------------------
    if not os.path.exists(os.path.join(FILTER_DIR, "Bawa_Categorized_Leads.csv")):
        log("STEP 4: Running AI Categorizer (Level 3)...")
        try:
            subprocess.run(["python", FILTER_3_SCRIPT], check=True, cwd=FILTER_DIR, timeout=14400)  # 4hr max
        except subprocess.TimeoutExpired:
            log("Categorizer timed out — partial output save ho gaya hoga.")
        except Exception as e:
            log(f"Categorizer error: {e}")
            return "ERROR"
    else:
        log("STEP 4: Already done — skipping.")

    # ---------------------------------------------------------
    # STEP 5: CLEANUP & ARCHIVE
    # ---------------------------------------------------------
    final_temp   = os.path.join(FILTER_DIR, "Bawa_Categorized_Leads.csv")
    archived_out = os.path.join(MASTER_DIR, f"Final_Extracted_Leads_{target_date_str}.csv")

    if os.path.exists(final_temp):
        shutil.move(final_temp, archived_out)
        log(f"🎉 DONE! VIP List saved: {archived_out}")

        # Clean workspace
        for temp_file in ["domain-names.txt", "premium_domains.txt", "Ultimate_God_Leads.csv",
                          "scanned_cache.txt", "filter_2.done", "date_lock.txt"]:
            tf = os.path.join(FILTER_DIR, temp_file)
            if os.path.exists(tf):
                os.remove(tf)

        log(f"Workspace clean. {target_date_str} closed ✅")
        return "SUCCESS"

    log("❌ Final output file missing — something went wrong.")
    return "ERROR"


# ==========================================
# 🔄 AUTOMATION ENGINE
# ==========================================
if __name__ == "__main__":
    log("Master Controller ONLINE.")

    while True:
        if not check_internet():
            log("No internet — sleeping 30s...")
            time.sleep(30)
            continue

        today_date = datetime.now().date()

        # ---------------------------------------------------------
        # Scenario 1: Resume incomplete work
        # FIX: Check karo exactly kahan ruka tha — seedha wahi se shuru
        # ---------------------------------------------------------
        if is_workspace_dirty():
            date_lock_file = os.path.join(FILTER_DIR, "date_lock.txt")
            if os.path.exists(date_lock_file):
                with open(date_lock_file, "r") as f:
                    locked_date = f.read().strip()
                log(f"🛑 BACKLOG: Resuming incomplete work for {locked_date}...")
                status = get_step_status()
                log(f"   Current status: {status}")
                fire_the_pipeline(locked_date, is_resume=True)
                continue
            else:
                # Lock file nahi hai — dirty files hain but date pata nahi
                # Safe cleanup karo
                log("⚠️  Dirty workspace but no lock file — cleaning up...")
                for temp_file in ["domain-names.txt", "premium_domains.txt",
                                  "scanned_cache.txt", "filter_2.done"]:
                    tf = os.path.join(FILTER_DIR, temp_file)
                    if os.path.exists(tf):
                        os.remove(tf)
                        log(f"   Removed: {temp_file}")
                continue

        # ---------------------------------------------------------
        # Scenario 2: Process all pending raw data (backlog dates)
        # ---------------------------------------------------------
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
                # Check if dummy/empty file
                try:
                    with open(final_csv_path, 'r', encoding='utf-8') as f:
                        content = f.read(200)
                        if "NO_DATA" in content or len(content) < 50:
                            needs_processing = True
                except:
                    needs_processing = True

            if needs_processing:
                log(f"🎯 Processing backlog date: {retro_str}")
                if os.path.exists(final_csv_path):
                    os.remove(final_csv_path)
                fire_the_pipeline(retro_str, is_resume=False)
                retro_action_taken = True
                break  # Ek date process karo, phir loop restart

        if retro_action_taken:
            continue

        # ---------------------------------------------------------
        # Scenario 3: Today's data
        # ---------------------------------------------------------
        target_str     = today_date.strftime("%Y-%m-%d")
        final_csv_path = os.path.join(MASTER_DIR, f"Final_Extracted_Leads_{target_str}.csv")

        if not os.path.exists(final_csv_path):
            log(f"Fetching today's data: {target_str}...")
            status = fire_the_pipeline(target_str, is_resume=False)
            if status == "MISSING":
                log(f"Aaj ({target_str}) ka data abhi WhoisDS pe nahi hai — waiting...")
                # Lock file clean karo taaki dirty workspace na lage
                lf = os.path.join(FILTER_DIR, "date_lock.txt")
                if os.path.exists(lf):
                    os.remove(lf)

        log("System idle — next check in 10 minutes...")
        time.sleep(600)