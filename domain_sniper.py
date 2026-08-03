import os
import cloudscraper
from bs4 import BeautifulSoup
from datetime import datetime
import zipfile
import re

# 🛑 BAWA YAHAN APNA FOLDER PATH DAAL DENA
SAVE_FOLDER = r"C:\Users\aksha\Documents\lead generation\daily_domains"

def sync_historical_whois_data():
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [+] System Waking Up... Scanning for Missing Historical Data.")
    
    if not os.path.exists(SAVE_FOLDER):
        os.makedirs(SAVE_FOLDER)
        print("[+] Created new secure vault (folder) for data.")
        
    url = "https://www.whoisds.com/newly-registered-domains"
    
    # 🚀 Cloudscraper Bypass
    scraper = cloudscraper.create_scraper(browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    })
    
    try:
        print("[+] Accessing WhoisDS Matrix (Bypassing Cloudflare)...")
        response = scraper.get(url)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 🎯 Website pe jo Table hai jisme dates hoti hain, usko target karte hain
        table = soup.find('table')
        if not table:
            print("[-] Alert: Table nahi mili website pe. DOM change ho gaya hai.")
            return

        rows = table.find_all('tr')
        downloaded_count = 0
        
        # Har row ko scan karo
        for row in rows:
            cols = row.find_all('td')
            if len(cols) >= 3: # Usually columns: Date, Domain Count, Download Link
                date_str = cols[0].text.strip()
                
                # Check karo ki format Date jaisa (YYYY-MM-DD) hi hai na
                if not re.match(r'\d{4}-\d{2}-\d{2}', date_str):
                    continue
                    
                expected_filename = f"Whois_Leads_Extracted_{date_str}.txt"
                expected_filepath = os.path.join(SAVE_FOLDER, expected_filename)
                
                # 🛡️ THE MISSING DATA CHECK
                if os.path.exists(expected_filepath):
                    print(f"[~] Safe: {date_str} ka data folder me pehle se hai. Skipping...")
                    continue
                    
                # Agar data missing hai, toh us specific row ka download link nikalo
                link_tag = row.find('a', href=True)
                if not link_tag:
                    continue
                    
                download_link = link_tag['href']
                
                # Kachra links ignore karo
                if 'whois-database-download' in download_link.lower() or 'contact' in download_link.lower():
                    continue
                    
                if not download_link.startswith('http'):
                    download_link = "https://www.whoisds.com" + download_link
                    
                print(f"\n[!] Missing Data Detected for: {date_str} ⚠️")
                print(f"[+] Target Locked! Downloading: {download_link}")
                
                # Download Process Shuru
                zip_file_name = f"temp_whois_{date_str}.zip"
                zip_file_path = os.path.join(SAVE_FOLDER, zip_file_name)
                
                file_response = scraper.get(download_link, stream=True)
                
                content_type = file_response.headers.get('Content-Type', '').lower()
                if 'text/html' in content_type:
                    print(f"[-] ALERT! HTML bheja server ne. Link issue hai. Skipping {date_str}...")
                    continue

                with open(zip_file_path, 'wb') as file:
                    for chunk in file_response.iter_content(chunk_size=8192):
                        if chunk:
                            file.write(chunk)
                            
                print(f"[+] ZIP Downloaded. Initiating Extraction...")

                # Extraction Process
                try:
                    with zipfile.ZipFile(zip_file_path, 'r') as zip_ref:
                        extracted_files = zip_ref.namelist()
                        if extracted_files:
                            original_file_name = extracted_files[0]
                            zip_ref.extract(original_file_name, SAVE_FOLDER)
                            original_file_path = os.path.join(SAVE_FOLDER, original_file_name)
                            
                            # Replace old file if somehow exist
                            if os.path.exists(expected_filepath):
                                os.remove(expected_filepath)
                                
                            # Rename to Date Specific Name
                            os.rename(original_file_path, expected_filepath)
                            print(f"[+] SUCCESS: Data saved as {expected_filename} ✅")
                            downloaded_count += 1
                except zipfile.BadZipFile:
                    print(f"[-] Error: {date_str} ki ZIP file corrupt hai.")
                    
                # Atomic Cleanup
                if os.path.exists(zip_file_path):
                    os.remove(zip_file_path)

        if downloaded_count == 0:
            print("\n[+] SYSTEM AUDIT PERFECT: Aapke folder me aage-peeche ka saara available data already UPDATE hai! 🛡️")
        else:
            print(f"\n[!] BINGPOT! Total {downloaded_count} missing dates ka data recover kar liya gaya hai 🚀")
            
    except Exception as e:
        print(f"[-] Error in the matrix: {e}")

if __name__ == "__main__":
    print("[+] Initiating Auto-Sync Historical Scanner NOW...")
    sync_historical_whois_data()