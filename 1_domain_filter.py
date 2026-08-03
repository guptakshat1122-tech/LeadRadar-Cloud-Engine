import time

# ==========================================
# BAWA'S UNIVERSAL NICHE DICTIONARY (God-Tier)
# ==========================================
INPUT_FILE = 'domain-names.txt'       
OUTPUT_FILE = 'premium_domains.txt'   

TARGET_KEYWORDS = [
    # Beauty & Skincare
    'beauty', 'skin', 'derma', 'glow', 'cosmetics', 'glam', 'care', 'lash', 'hair', 'salon', 'aura', 'luxe', 'pure', 'face',
    # Kids & Maternity
    'kids', 'baby', 'child', 'tots', 'little', 'junior', 'mama', 'toy', 'play', 'tiny', 'cradle', 'mom',
    # Fashion & Apparel
    'wear', 'style', 'vogue', 'thread', 'apparel', 'outfit', 'stitch', 'trend', 'closet', 'garb', 'kicks',
    # Tech, SaaS & AI
    'tech', 'saas', 'ai', 'app', 'labs', 'api', 'dev', 'cloud', 'cyber', 'data', 'hq', 'soft', 'code', 'byte', 'stack', 'bot', 'sync', 'flow',
    # Pets
    'pet', 'paw', 'tail', 'vet', 'bark', 'fur', 'meow', 'hound',
    # Home & Decor
    'home', 'decor', 'living', 'space', 'nest', 'craft', 'furn', 'wood', 'casa', 'room', 'vibe',
    # E-Commerce & Retail
    'shop', 'store', 'cart', 'buy', 'mart', 'brand', 'goods', 'deal', 'loot', 'retail',
    # Health & Fitness
    'health', 'fit', 'med', 'clinic', 'cure', 'gym', 'wellness', 'dental', 'nutra', 'diet', 'vital', 'protein',
    # Finance & Crypto
    'pay', 'fin', 'wealth', 'invest', 'capital', 'fund', 'trade', 'crypto', 'coin', 'mint', 'tax', 'bank',
    # Real Estate & Agencies
    'realty', 'estate', 'prop', 'homes', 'build', 'infra', 'arch', 'land', 'agency', 'studio', 'media', 'consult', 'partners', 'group', 'growth', 'creative', 'event', 'wed', 'bliss',
    # Food & EdTech
    'food', 'eats', 'brew', 'cafe', 'farm', 'fresh', 'bite', 'agro', 'dine', 'snack', 'sip', 'learn', 'edu', 'academy', 'skill', 'prep', 'brain', 'tutor', 'class'
]

TARGET_TLDS = [
    '.in', '.com', '.co', '.co.in', '.io', '.ai', '.app', '.tech', 
    '.health', '.store', '.shop', '.so', '.gg', '.net', '.org', '.co.uk', '.us'
]

def is_premium(domain):
    domain = domain.strip().lower()
    parts = domain.split('.')
    if len(parts) < 2:
        return False
        
    root_name = parts[0]
    
    # 1. Length Check (Relaxed to 22 characters for long brand names)
    if len(root_name) > 22 or len(root_name) < 3: 
        return False
        
    # 2. Formatting Check (Max 1 dash, max 3 numbers)
    if root_name.count('-') > 1 or sum(c.isdigit() for c in root_name) > 3: 
        return False
    
    # 3. Quality Check (Must have a premium TLD OR a target keyword)
    has_premium_tld = any(domain.endswith(tld) for tld in TARGET_TLDS)
    has_keyword = any(keyword in root_name for keyword in TARGET_KEYWORDS)
    
    if has_premium_tld or has_keyword: 
        return True
        
    return False

def main():
    print("🧹 [FILTER ENGINE] Bawa's Universal Filter running...\n")
    premium_domains = []
    total_scanned = 0
    
    try:
        with open(INPUT_FILE, 'r', encoding='utf-8') as file:
            for line in file:
                total_scanned += 1
                domain = line.strip()
                if is_premium(domain): 
                    premium_domains.append(domain)
    except FileNotFoundError:
        print(f"❌ Error: '{INPUT_FILE}' file current folder me nahi mili!")
        print("   -> WhoisDS se extract karke yahan paste kar bawa.")
        return

    # Filtered leads ko nayi file me save karna
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as out_file:
        for d in premium_domains: 
            out_file.write(f"{d}\n")

    print(f"✅ Scanning Done! Total Domains Checked: {total_scanned}")
    print(f"💎 Premium Leads Extracted: {len(premium_domains)}")
    print(f"📁 Jaa kar '{OUTPUT_FILE}' check kar. Kachra saaf ho chuka hai!")

if __name__ == "__main__":
    start_time = time.time()
    main()
    print(f"⏱️ Time taken: {round(time.time() - start_time, 2)} seconds")
