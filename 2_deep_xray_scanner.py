import csv
import requests
import concurrent.futures
import re
from bs4 import BeautifulSoup
from urllib3.exceptions import InsecureRequestWarning
import time
import os
import threading
try:
    from deep_translator import GoogleTranslator
    TRANSLATOR_AVAILABLE = True
except ImportError:
    TRANSLATOR_AVAILABLE = False
    print("⚠️  deep-translator not installed. Run: pip install deep-translator")

def translate_to_english(text, max_len=400):
    """
    Foreign language text ko English mein translate karo.
    85%+ ASCII ho toh skip — already English/Roman hai.
    """
    if not text or len(text.strip()) < 10:
        return text

    # Already English/Roman check
    ascii_ratio = sum(1 for c in text if ord(c) < 128) / len(text)
    if ascii_ratio > 0.85:
        return text

    if not TRANSLATOR_AVAILABLE:
        return text  # Library nahi hai — original return karo

    try:
        translated = GoogleTranslator(source='auto', target='en').translate(text[:max_len])
        return translated if translated else text
    except Exception:
        return text  # Translate fail — original wapas do

requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

# ==========================================
# BAWA'S SMART RESUME X-RAY SCANNER v2.0
# NOW WITH: Business Type + Product Category Detection
# ==========================================
INPUT_FILE = 'premium_domains.txt'
OUTPUT_FILE = 'Ultimate_God_Leads.csv'
CACHE_FILE = 'scanned_cache.txt'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
}

# 2 naye columns add kiye: Business_Type aur Product_Category
KEYS = ['Domain', 'Brand_Stage', 'Title', 'Meta_Description', 'Page_Text', 'Emails', 'Phones', 'Socials_Found', 'Tech_Stack_Ads', 'Intent_Trust', 'Business_Type', 'Product_Category']
FILE_LOCK = threading.Lock()


# ==========================================
# 🧠 BUSINESS TYPE CLASSIFIER
# ==========================================
def classify_business(text, nav_links):
    nav_text = ' '.join(nav_links)
    combined = text + ' ' + nav_text

    # SaaS / Software — highest priority
    saas_words = [
        'free trial', 'start free', 'book a demo', 'request a demo', 'schedule a demo',
        'dashboard', 'login', 'sign up free', 'api', 'integration', 'software',
        'platform', 'saas', 'automate', 'workflow', 'crm', 'erp', 'subscription plan',
        'monthly plan', 'annual plan', 'upgrade plan'
    ]
    if any(w in combined for w in saas_words):
        return '💻 SaaS / Software'

    # Startup — check before service/product
    startup_words = [
        'join the waitlist', 'join waitlist', 'get early access', 'early access',
        'we are building', 'we\'re building', 'backed by', 'seed round', 'series a',
        'pre-seed', 'in beta', 'beta access', 'founding team', 'we raised',
        'investor', 'venture', 'launching soon', 'notify me when'
    ]
    if any(w in combined for w in startup_words):
        return '🚀 Startup'

    # Physical Product (E-Commerce)
    product_words = [
        'add to cart', 'add to bag', 'buy now', 'shop now', 'shop the collection',
        'free shipping', 'order now', 'in stock', 'out of stock', 'checkout',
        'collection', 'all products', 'new arrivals', 'best sellers', 'shop all',
        'cod available', 'cash on delivery', 'track your order', 'return policy'
    ]
    if any(w in combined for w in product_words):
        return '📦 Physical Product Brand'

    # Service Based Business
    service_words = [
        'book a call', 'book a free call', 'get a quote', 'free quote', 'hire us',
        'our services', 'what we do', 'our work', 'case studies', 'consultation',
        'agency', 'we help', 'we specialize', 'our process', 'portfolio',
        'client results', 'work with us', 'let\'s talk', 'get in touch'
    ]
    if any(w in combined for w in service_words):
        return '🛠️ Service Based'

    return '❓ Unclear'


# ==========================================
# 🏷️ PRODUCT / NICHE CATEGORY CLASSIFIER
# ==========================================
def classify_product(text):
    categories = [
        ('👗 Fashion & Apparel',    ['clothing', 'fashion', 'apparel', 'wear', 'outfit', 'dress', 'shirt', 't-shirt', 'hoodie', 'shoes', 'sneakers', 'footwear', 'collection', 'wardrobe', 'streetwear', 'ethnic wear', 'kurta', 'saree']),
        ('💄 Beauty & Skincare',    ['skincare', 'beauty', 'serum', 'moisturizer', 'cosmetic', 'glow', 'hair care', 'shampoo', 'conditioner', 'lip', 'foundation', 'makeup', 'nail', 'fragrance', 'perfume', 'sunscreen', 'face wash']),
        ('🍔 Food & Beverage',      ['food', 'snack', 'beverage', 'drink', 'coffee', 'tea', 'nutrition', 'protein', 'chocolate', 'biscuit', 'sauce', 'spice', 'organic food', 'health food', 'meal', 'recipe', 'restaurant', 'cafe', 'bakery']),
        ('🐾 Pets',                 ['pet', 'dog', 'cat', 'paw', 'fur', 'vet', 'animal', 'puppy', 'kitten', 'pet food', 'pet care', 'grooming']),
        ('🏠 Home & Decor',         ['home decor', 'furniture', 'interior', 'living room', 'bedroom', 'candle', 'wall art', 'cushion', 'lamp', 'rug', 'curtain', 'mattress', 'sofa', 'kitchen', 'bathroom', 'storage']),
        ('💪 Health & Fitness',     ['fitness', 'gym', 'supplement', 'workout', 'wellness', 'yoga', 'diet', 'weight loss', 'muscle', 'protein powder', 'pre workout', 'health', 'ayurved', 'naturo', 'immunity', 'vitamin', 'omega']),
        ('👶 Kids & Baby',          ['kids', 'baby', 'toddler', 'children', 'toy', 'parenting', 'infant', 'newborn', 'maternity', 'diaper', 'stroller', 'school bag', 'kids wear']),
        ('💻 Tech & Gadgets',       ['gadget', 'device', 'electronics', 'wireless', 'smart home', 'charger', 'earphone', 'headphone', 'laptop', 'mobile', 'phone case', 'power bank', 'camera', 'drone', 'wearable', 'smartwatch']),
        ('📚 Education & Coaching', ['course', 'learn', 'education', 'training', 'skill', 'tutorial', 'coaching', 'mentor', 'certification', 'bootcamp', 'masterclass', 'workshop', 'study', 'exam prep', 'upskill']),
        ('💰 Finance & Fintech',    ['invest', 'finance', 'crypto', 'trading', 'wealth', 'insurance', 'loan', 'mutual fund', 'stock', 'portfolio', 'fintech', 'banking', 'payment', 'wallet', 'tax', 'accounting']),
        ('🏡 Real Estate',          ['real estate', 'property', 'flat', 'apartment', 'villa', 'plot', 'buy home', 'rent', 'commercial space', 'office space', 'realty', 'housing']),
        ('✈️ Travel & Hospitality', ['travel', 'hotel', 'resort', 'holiday', 'vacation', 'tour', 'trek', 'adventure', 'flight', 'booking', 'airbnb', 'hospitality', 'staycay']),
        ('🎮 Gaming & Entertainment',['gaming', 'game', 'esports', 'streaming', 'entertainment', 'music', 'podcast', 'creator', 'content', 'media', 'film', 'video']),
        ('🌿 Sustainable & Eco',    ['sustainable', 'eco', 'organic', 'green', 'zero waste', 'recyclable', 'environment', 'natural', 'vegan', 'cruelty free', 'biodegradable']),
    ]

    for category, keywords in categories:
        if any(kw in text for kw in keywords):
            return category

    return '🌐 General / Other'


# ==========================================
# 🔍 MAIN EXTRACTOR FUNCTION
# ==========================================
def extract_advanced_data(domain):
    domain = domain.strip()
    if not domain:
        return None

    url = f"http://{domain}"
    lead_data = {
        'Domain': domain,
        'Brand_Stage': 'Live',
        'Title': 'None',
        'Meta_Description': 'None',
        'Page_Text': 'None',
        'Emails': 'None',
        'Phones': 'None',
        'Socials_Found': 'None',
        'Tech_Stack_Ads': 'None',
        'Intent_Trust': 'None',
        'Business_Type': 'None',
        'Product_Category': 'None',
        'Status': 'Dead'
    }

    try:
        response = requests.get(url, headers=HEADERS, timeout=(4, 5), verify=False)

        if response.status_code == 200:
            lead_data['Status'] = 'LIVE'
            html_content = response.text
            soup = BeautifulSoup(html_content, 'html.parser')
            text_lower = html_content.lower()

            # ------------------------------------------
            # 1. Brand Stage
            # ------------------------------------------
            stage_signals = []
            if "coming soon" in text_lower or "launching soon" in text_lower:
                stage_signals.append("Pre-Launch")
            if "password" in text_lower and "shopify" in text_lower:
                stage_signals.append("Shopify Password Page")
            if "linktree" in text_lower or "bento.me" in text_lower:
                stage_signals.append("Using Link-in-Bio")
            if stage_signals:
                lead_data['Brand_Stage'] = " | ".join(stage_signals)

            # ------------------------------------------
            # 2. Title
            # ------------------------------------------
            title_tag = soup.find('title')
            if title_tag and title_tag.text:
                lead_data['Title'] = title_tag.text.strip()[:60]

            # ------------------------------------------
            # 3. Emails
            # ------------------------------------------
            email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
            found_emails = set(re.findall(email_pattern, html_content))
            clean_emails = [e for e in found_emails if not any(x in e for x in ['sentry', 'wix', 'example', 'test', 'domain'])]
            if clean_emails:
                lead_data['Emails'] = " | ".join(clean_emails)

            # ------------------------------------------
            # 4. Phones
            # ------------------------------------------
            phone_pattern = r'(?:(?:\+|00)\d{1,3}[\s-]?)?(?:\(?\d{3}\)?[\s-]?)?\d{3}[\s-]?\d{4}'
            found_phones = set(re.findall(phone_pattern, html_content))
            clean_phones = [p for p in found_phones if 10 <= len(re.sub(r'\D', '', p)) <= 15]
            if clean_phones:
                lead_data['Phones'] = " | ".join(list(clean_phones)[:2])

            # ------------------------------------------
            # 5. Socials
            # ------------------------------------------
            found_platforms = []
            for a in soup.find_all('a', href=True):
                href = a.get('href', '').lower()
                if 'instagram.com' in href:
                    found_platforms.append("Instagram")
                elif 'linkedin.com/company' in href:
                    found_platforms.append("LinkedIn")
                elif 'youtube.com' in href:
                    found_platforms.append("YouTube")
                elif 'twitter.com' in href or 'x.com' in href:
                    found_platforms.append("Twitter/X")
                elif 'facebook.com' in href:
                    found_platforms.append("Facebook")
                elif 'tiktok.com' in href:
                    found_platforms.append("TikTok")
            if found_platforms:
                lead_data['Socials_Found'] = " + ".join(set(found_platforms))

            # ------------------------------------------
            # 6. Tech & Ads
            # ------------------------------------------
            tech_found = []
            if "fbq('init'" in text_lower or "facebook pixel" in text_lower:
                tech_found.append("Meta Ads")
            if "gtag(" in text_lower or "google-analytics" in text_lower:
                tech_found.append("Google Ads")
            if "shopify" in text_lower:
                tech_found.append("Shopify")
            if "webflow" in text_lower:
                tech_found.append("Webflow")
            if "klaviyo" in text_lower:
                tech_found.append("Klaviyo")
            if "tiktok" in text_lower and "pixel" in text_lower:
                tech_found.append("TikTok Ads")
            if tech_found:
                lead_data['Tech_Stack_Ads'] = " + ".join(tech_found)

            # ------------------------------------------
            # 7. Intent
            # ------------------------------------------
            intent_found = []
            if "add to cart" in text_lower or "checkout" in text_lower:
                intent_found.append("E-Com")
            if "book a demo" in text_lower or "request demo" in text_lower:
                intent_found.append("SaaS/B2B")
            if intent_found:
                lead_data['Intent_Trust'] = " | ".join(intent_found)

            # ------------------------------------------
            # 8. 🆕 Business Type & Product Category
            # ------------------------------------------
            # Meta description nikalo
            meta_desc = ''
            meta_tag = soup.find('meta', attrs={'name': 'description'})
            if meta_tag:
                meta_desc = meta_tag.get('content', '')[:300]
            if not meta_desc:
                og_tag = soup.find('meta', attrs={'property': 'og:description'})
                if og_tag:
                    meta_desc = og_tag.get('content', '')[:300]

            # ✅ Smart body text — meaningful sentences only, no repetition
            all_text = soup.get_text(separator=' ', strip=True)
            # Split into sentences/phrases, deduplicate, keep meaningful ones
            seen_phrases = set()
            meaningful = []
            for part in re.split(r'[.!?\n|•·]', all_text):
                part = part.strip()
                part_lower = part.lower()
                if (len(part) > 20 and              # too short = useless
                    part_lower not in seen_phrases and  # no repeats
                    not part_lower.startswith(domain.split('.')[0])): # skip "domain domain domain"
                    seen_phrases.add(part_lower)
                    meaningful.append(part)
                if len(' '.join(meaningful)) > 600:
                    break
            body_text = ' '.join(meaningful)[:600].lower()
            lead_data['Page_Text'] = translate_to_english(body_text)[:500]

            # Nav/button links
            nav_links = []
            for a in soup.find_all(['a', 'button'], href=True):
                txt = a.get_text(strip=True).lower()
                if txt and len(txt) < 40:
                    nav_links.append(txt)

            # ✅ Save meta for AI categorizer (translate if foreign)
            if meta_desc:
                meta_clean = meta_desc.strip()[:400]
                lead_data['Meta_Description'] = translate_to_english(meta_clean)[:300]

            # Combined text for rule-based classification
            classify_text = (meta_desc + ' ' + body_text).lower()

            lead_data['Business_Type'] = classify_business(classify_text, nav_links)
            lead_data['Product_Category'] = classify_product(classify_text)

            return lead_data

    except Exception:
        pass

    return None


# ==========================================
# 💾 PROCESS & SAVE
# ==========================================
def process_and_save(domain):
    result = extract_advanced_data(domain)

    with FILE_LOCK:
        with open(CACHE_FILE, 'a', encoding='utf-8') as cache:
            cache.write(domain + '\n')

        if result and result['Status'] == 'LIVE':
            if (result['Emails'] != 'None' or
                result['Socials_Found'] != 'None' or
                result['Tech_Stack_Ads'] != 'None' or
                result['Brand_Stage'] != 'Live'):

                file_exists = os.path.exists(OUTPUT_FILE)
                with open(OUTPUT_FILE, 'a', newline='', encoding='utf-8') as csvfile:
                    writer = csv.DictWriter(csvfile, fieldnames=KEYS)
                    if not file_exists or os.path.getsize(OUTPUT_FILE) == 0:
                        writer.writeheader()
                    writer.writerow({k: result[k] for k in KEYS})


# ==========================================
# 🚀 MAIN
# ==========================================
def main():
    print("🔥 [SMART RESUME X-RAY v2.0 ON] System Booting...\n")
    print("🧠 NEW: Business Type + Product Category detection ACTIVE\n")

    try:
        with open(INPUT_FILE, 'r', encoding='utf-8') as file:
            all_domains = set([line.strip() for line in file if line.strip()])
    except FileNotFoundError:
        print(f"❌ Error: '{INPUT_FILE}' nahi mili.")
        return

    scanned_domains = set()
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r', encoding='utf-8') as file:
            scanned_domains = set([line.strip() for line in file if line.strip()])

    domains_to_scan = list(all_domains - scanned_domains)
    total_left = len(domains_to_scan)

    print(f"📊 Total Domains in list: {len(all_domains)}")
    print(f"⏭️  Already Scanned (Skipping): {len(scanned_domains)}")
    print(f"🎯 Target Domains for this session: {total_left}")

    if 0 < total_left <= 5:
        print(f"⚠️  [WARNING] These domains were previously stuck: {domains_to_scan}")

    if total_left == 0:
        print("✅ Saare domains already scan ho chuke hain! Moving to next step.")
        open("filter_2.done", "w").close()
        return

    processed_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=60) as executor:
        futures = {executor.submit(process_and_save, domain): domain for domain in domains_to_scan}

        for future in concurrent.futures.as_completed(futures):
            processed_count += 1
            if processed_count % 5 == 0 or processed_count == total_left:
                print(f"🚀 Speedometer: {processed_count}/{total_left} domains checked...", end='\r', flush=True)

            if processed_count % 500 == 0:
                print(f"\n✅ [CHECKPOINT] {processed_count} domains done. Data Live Saved!")

    print(f"\n\n💾 Scanning 100% Complete for today!")
    open("filter_2.done", "w").close()


if __name__ == "__main__":
    start_time = time.time()
    main()
    print(f"⏱️  Time taken: {round(time.time() - start_time, 2)} seconds")
