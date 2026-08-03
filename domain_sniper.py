import os
import sys
import cloudscraper
from bs4 import BeautifulSoup
from datetime import datetime
import zipfile
import re

# 📁 Repo-relative — script jahan bhi checkout ho, wahi ke andar "daily_domains" folder banega
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
SAVE_FOLDER = os.path.join(BASE_DIR, "daily_domains")


def sync_historical_whois_data(target_date_str=None):
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [+] System Waking Up... Scanning for Missing Historical Data.")
    if target_date_str:
        print(f"[+] Targeted mode: sirf {target_date_str} ka data dhoondenge.")

    if not os.path.exists(SAVE_FOLDER):
        os.makedirs(SAVE_FOLDER)
        print("[+] Created new secure vault (folder) for data.")

    url = "https://www.whoisds.com/newly-registered-domains"

    scraper = cloudscraper.create_scraper(browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    })

    try:
        print("[+] Accessing WhoisDS Matrix (Bypassing Cloudflare)...")
        response = scraper.get(url, timeout=30)
        soup = BeautifulSoup(response.text, 'html.parser')

        table = soup.find('table')
        if not table:
            print("[-] Alert: Table nahi mili website pe. DOM change ho gaya hai.")
            return False

        rows = table.find_all('tr')
        downloaded_count = 0
        target_found_and_ready = False

        for row in rows:
            cols = row.find_all('td')
            if len(cols) >= 3:
                date_str = cols[0].text.strip()

                if not re.match(r'\d{4}-\d{2}-\d{2}', date_str):
                    continue

                # Targeted mode me sirf wahi date process karo, baaki skip
                if target_date_str and date_str != target_date_str:
                    continue

                expected_filename = f"Whois_Leads_Extracted_{date_str}.txt"
                expected_filepath = os.path.join(SAVE_FOLDER, expected_filename)

                if os.path.exists(expected_filepath):
                    print(f"[~] Safe: {date_str} ka data folder me pehle se hai. Skipping...")
                    if target_date_str and date_str == target_date_str:
                        target_found_and_ready = True
                    continue

                link_tag = row.find('a', href=True)
                if not link_tag:
                    continue

                download_link = link_tag['href']

                if 'whois-database-download' in download_link.lower() or 'contact' in download_link.lower():
                    continue

                if not download_link.startswith('http'):
                    download_link = "https://www.whoisds.com" + download_link

                print(f"\n[!] Missing Data Detected for: {date_str} ⚠️")
                print(f"[+] Target Locked! Downloading: {download_link}")

                zip_file_name = f"temp_whois_{date_str}.zip"
                zip_file_path = os.path.join(SAVE_FOLDER, zip_file_name)

                file_response = scraper.get(download_link, stream=True, timeout=60)

                content_type = file_response.headers.get('Content-Type', '').lower()
                if 'text/html' in content_type:
                    print(f"[-] ALERT! HTML bheja server ne. Link issue hai. Skipping {date_str}...")
                    continue

                with open(zip_file_path, 'wb') as file:
                    for chunk in file_response.iter_content(chunk_size=8192):
                        if chunk:
                            file.write(chunk)

                print(f"[+] ZIP Downloaded. Initiating Extraction...")

                try:
                    with zipfile.ZipFile(zip_file_path, 'r') as zip_ref:
                        extracted_files = zip_ref.namelist()
                        if extracted_files:
                            original_file_name = extracted_files[0]
                            zip_ref.extract(original_file_name, SAVE_FOLDER)
                            original_file_path = os.path.join(SAVE_FOLDER, original_file_name)

                            if os.path.exists(expected_filepath):
                                os.remove(expected_filepath)

                            os.rename(original_file_path, expected_filepath)
                            print(f"[+] SUCCESS: Data saved as {expected_filename} ✅")
                            downloaded_count += 1
                            if target_date_str and date_str == target_date_str:
                                target_found_and_ready = True
                except zipfile.BadZipFile:
                    print(f"[-] Error: {date_str} ki ZIP file corrupt hai.")

                if os.path.exists(zip_file_path):
                    os.remove(zip_file_path)

                # Targeted mode me apna kaam ho gaya, baaki rows check karne ki zaroorat nahi
                if target_date_str and date_str == target_date_str:
                    break

        if downloaded_count == 0 and not target_found_and_ready:
            print("\n[+] Koi naya data nahi mila is run me.")
        else:
            print(f"\n[!] Total {downloaded_count} date(s) ka data is run me download hua.")

        if target_date_str:
            return target_found_and_ready or os.path.exists(
                os.path.join(SAVE_FOLDER, f"Whois_Leads_Extracted_{target_date_str}.txt")
            )
        return True

    except Exception as e:
        print(f"[-] Error in the matrix: {e}")
        return False


if __name__ == "__main__":
    print("[+] Initiating Auto-Sync Historical Scanner NOW...")
    requested_date = sys.argv[1].strip() if len(sys.argv) > 1 else None
    ok = sync_historical_whois_data(requested_date)

    # 🔑 Exit code matters — master_controller.py isko check karta hai
    # (check=True + CalledProcessError se "MISSING" detect hota hai)
    if requested_date:
        sys.exit(0 if ok else 1)
    sys.exit(0 if ok else 1)
