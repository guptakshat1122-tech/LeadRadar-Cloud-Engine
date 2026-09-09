import csv
import os
import json
import time
import sys
import requests
import re

# ==========================================
# ⚙️ CONFIGURATION — ab Groq cloud API use karta hai (koi local Ollama nahi chahiye)
# ==========================================
INPUT_FILE   = 'Ultimate_God_Leads.csv'
OUTPUT_FILE  = 'Bawa_Categorized_Leads.csv'
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
MODEL_NAME   = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")
BATCH_SIZE   = 5          # Groq cloud fast hai, Ollama jaisa 2 rakhne ki zaroorat nahi
MAX_RETRIES  = 3          # sirf REAL errors ke liye (rate-limit alag se handle hota hai)
MAX_RATE_LIMIT_RETRIES = 8  # rate-limit koi "failure" nahi hai, isliye zyada patience

# Adaptive spacing — agar rate-limit baar baar lage to batches ke beech gap khud badhega
BASE_BATCH_SLEEP = 0.5
MAX_BATCH_SLEEP  = 12.0
current_batch_sleep = BASE_BATCH_SLEEP
consecutive_rate_limits = 0

stats = {"processed": 0, "auto_done": 0, "ai_skipped": 0, "retries": 0, "rate_limit_hits": 0, "json_fallback_saved": 0}

NAV_WORDS = {
    "home", "about", "contact", "menu", "toggle", "navigation", "nav",
    "skip", "close", "open", "search", "cart", "login", "register",
    "signup", "next", "previous", "back", "read", "more", "click",
    "here", "cookie", "privacy", "policy", "terms", "copyright",
    "all", "rights", "reserved", "powered", "inc", "llc", "ltd",
    "password", "enter", "sign", "get", "started", "learn"
}

def extract_content(text, domain=""):
    """Meaningful words only — nav garbage hatao."""
    if not text or text.strip().lower() in ("none", ""):
        return ""
    text = text.encode("utf-8", errors="ignore").decode("utf-8")
    text = "".join(c for c in text if c.isprintable() and ord(c) < 65536)
    domain_root = re.sub(r'[^a-z0-9]', '', domain.split(".")[0].lower()) if domain else ""
    words = re.findall(r'[a-zA-Z]{3,}', text)
    seen = {}
    clean = []
    for w in words:
        wl = w.lower()
        if wl in NAV_WORDS or wl == domain_root:
            continue
        seen[wl] = seen.get(wl, 0) + 1
        if seen[wl] <= 2:
            clean.append(w)
        if len(clean) >= 80:
            break
    return " ".join(clean)

def clean_field(text, max_len=250):
    if not text or text.strip().lower() in ("none", ""):
        return ""
    text = text.encode("utf-8", errors="ignore").decode("utf-8")
    text = "".join(c for c in text if c.isprintable() and ord(c) < 65536)
    return text.replace("\\", " ").replace('"', "'").strip()[:max_len]

# ==========================================
# ⚡ INSTANT RULES
# ==========================================
PARKED_SIGNALS    = ["parked domain", "hostinger dns", "domain for sale", "buy this domain",
                     "hugedomains", "sedoparking", "undeveloped", "welcome to nginx"]
PRELAUNCH_SIGNALS = ["launching soon", "coming soon", "under construction",
                     "check back for an update", "being worked on", "we're under construction",
                     "opening soon", "be the first to know when we launch"]

def is_parked(lead):
    combined = ((lead.get("Title","") or "") + " " + (lead.get("Page_Text","") or "")).lower()
    return any(s in combined for s in PARKED_SIGNALS)

def is_prelaunch(lead):
    stage    = (lead.get("Brand_Stage","") or "").lower()
    combined = ((lead.get("Title","") or "") + " " + (lead.get("Page_Text","") or "")).lower()
    return stage == "pre-launch" or any(s in combined for s in PRELAUNCH_SIGNALS)

def has_no_content(lead):
    title = clean_field(lead.get("Title",""))
    meta  = clean_field(lead.get("Meta_Description",""))
    cont  = extract_content(lead.get("Page_Text",""), lead.get("Domain",""))
    return len(title + meta + cont) < 10

# ==========================================
# 🔧 CATEGORY NORMALIZER
# ==========================================
def normalize_category(pitch, domain="", biz="", prod=""):
    """Fix malformed categories — replace (NICHE) with actual industry, fix wrong emojis."""
    if not pitch:
        return "🟢 6. General Contacts"

    EMOJI_FIX = {
        "🍺": "🎬", "📝": "🛠️", "🎥": "🎬", "📱": "🤖",
        "💻": "🤖", "🏥": "🛠️", "🍔": "🛠️", "🏠": "🛠️",
        "🌿": "💰", "👗": "💰", "🎓": "🛠️", "⚖️": "🛠️",
    }
    for wrong, right in EMOJI_FIX.items():
        if pitch.startswith(wrong):
            pitch = right + pitch[len(wrong):]

    pitch_lower = pitch.lower()
    INVENTED = ["author", "blogger", "writer", "poet"]
    for inv in INVENTED:
        if pitch_lower.startswith(inv) or f" {inv}" in pitch_lower[:15]:
            niche_match = re.search(r"\(.*?\)", pitch)
            niche = niche_match.group(0) if niche_match else ""
            pitch = f"🛠️ 5. Service Agencies {niche}".strip()
            break

    if "(NICHE)" in pitch or "(niche)" in pitch.lower():
        hint = (biz + " " + prod).lower()
        if any(x in hint for x in ["health", "medical", "clinic", "doctor", "pharma", "dental"]):
            niche = "Healthcare"
        elif any(x in hint for x in ["fitness", "gym", "sport", "athlet", "workout"]):
            niche = "Fitness"
        elif any(x in hint for x in ["fashion", "cloth", "wear", "apparel", "textile"]):
            niche = "Fashion"
        elif any(x in hint for x in ["food", "restaurant", "cafe", "dining", "kitchen", "catering"]):
            niche = "Food & Beverage"
        elif any(x in hint for x in ["tech", "software", "saas", "ai", "digital", "cloud"]):
            niche = "Tech"
        elif any(x in hint for x in ["real estate", "property", "realty", "housing"]):
            niche = "Real Estate"
        elif any(x in hint for x in ["market", "agency", "seo", "ads", "creative"]):
            niche = "Marketing"
        elif any(x in hint for x in ["educat", "school", "learn", "tutor", "academy"]):
            niche = "Education"
        elif any(x in hint for x in ["beauty", "skin", "cosmetic", "salon", "hair"]):
            niche = "Beauty"
        elif any(x in hint for x in ["legal", "law", "lawyer", "attorney"]):
            niche = "Legal"
        elif any(x in hint for x in ["finance", "invest", "wealth", "banking", "insurance"]):
            niche = "Finance"
        else:
            niche = domain.split(".")[0].title() if domain else "General"
        pitch = re.sub(r'\(NICHE\)', f"({niche})", pitch, flags=re.IGNORECASE)

    return pitch

# ==========================================
# 🩺 STARTUP SANITY CHECK — pehle hi pata chal jaye galti kahan hai
# ==========================================
def run_preflight_check():
    print("🩺 Preflight check chal raha hai...")

    # 1) Key loaded hai ya nahi
    if not GROQ_API_KEY:
        print("❌ GROQ_API_KEY khaali hai! Env var / GitHub secret set nahi hua.")
        sys.exit(1)
    else:
        masked = GROQ_API_KEY[:4] + "..." + GROQ_API_KEY[-4:] if len(GROQ_API_KEY) > 8 else "***"
        print(f"   ✅ GROQ_API_KEY mil gayi (length={len(GROQ_API_KEY)}, {masked})")

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}

    # 2) Key valid hai + model list fetch karke dekho MODEL_NAME available hai ya nahi
    try:
        resp = requests.get(GROQ_MODELS_URL, headers=headers, timeout=30)
    except requests.exceptions.RequestException as e:
        print(f"❌ Groq se connect hi nahi ho paaya: {e}")
        sys.exit(1)

    if resp.status_code == 401:
        print("❌ GROQ_API_KEY invalid/expired hai (401 Unauthorized).")
        print(f"   ↳ Response: {resp.text[:300]}")
        sys.exit(1)

    if resp.status_code != 200:
        print(f"❌ Model list fetch nahi ho payi — status {resp.status_code}")
        print(f"   ↳ Response: {resp.text[:300]}")
        sys.exit(1)

    try:
        available_models = [m["id"] for m in resp.json().get("data", [])]
    except Exception as e:
        print(f"⚠️  Model list parse nahi ho payi: {e}")
        available_models = []

    print(f"   ✅ API key valid hai. {len(available_models)} models available hain.")

    if available_models and MODEL_NAME not in available_models:
        print(f"❌ MODEL_NAME '{MODEL_NAME}' Groq ke available models me nahi hai!")
        print(f"   ↳ Available models: {available_models}")
        print("   👉 GROQ_MODEL env var ko available list me se koi valid model id set karo.")
        sys.exit(1)
    else:
        print(f"   ✅ Model '{MODEL_NAME}' available hai.")

    # 3) Ek dummy real request bhi maar ke dekho — end-to-end confirm
    test_payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": "Reply with just the word OK."}],
        "max_tokens": 5
    }
    try:
        test_resp = requests.post(GROQ_API_URL, json=test_payload,
                                   headers={**headers, "Content-Type": "application/json"},
                                   timeout=30)
    except requests.exceptions.RequestException as e:
        print(f"❌ Test chat completion request fail ho gayi: {e}")
        sys.exit(1)

    if test_resp.status_code != 200:
        print(f"❌ Test chat completion fail — status {test_resp.status_code}")
        print(f"   ↳ Response: {test_resp.text[:500]}")
        sys.exit(1)

    print("   ✅ Test chat completion successful. Sab thik hai, aage badhte hain.\n")


# ==========================================
# 🧠 AI CATEGORIZER — Groq cloud (OpenAI-compatible) API
# ==========================================
SYSTEM_PROMPT = """You are a business analyst. Read each website's text and categorize it.

Use ALL text signals: title, meta description, and content keywords.
Even if content is short, make your best guess from domain name + title + meta.

OUTPUT — use EXACTLY these formats:
- "🔥 1. Pre-Launch" — coming soon, not launched
- "🤖 2. SaaS/Tech (NICHE)" — software, app, platform, tool, AI product
- "💰 3. D2C Ad Spenders (NICHE)" — physical product brand selling online
- "🎬 4. Video-First Brands (NICHE)" — video company, media studio, YouTube channel
- "🛠️ 5. Service Agencies (NICHE)" — agency, consultant, doctor, restaurant, school, local service
- "🟢 6. General Contacts" — ONLY if truly zero signals, absolute last resort

Replace NICHE with real industry. Examples:
"🤖 2. SaaS/Tech (Personality Analytics)"
"🛠️ 5. Service Agencies (Influencer Marketing)"
"💰 3. D2C Ad Spenders (Organic Fashion)"
"🤖 2. SaaS/Tech (AI Robotics)"

RULES:
- NEVER return "(NICHE)" literally — always replace with actual industry
- NEVER return Unknown for business_type if you have any signals
- If content is foreign language, use domain+title to guess
- Return items in SAME ORDER as input using "index"

Return ONLY a raw JSON object with this exact shape (no markdown fences, no extra text):
{"items": [{"index":0,"domain":"x.com","pitch_category":"🛠️ 5. Service Agencies (Healthcare)","true_business_type":"Orthopaedic Clinic","true_product_category":"Joint Replacement Surgery"}]}
"""

def categorize_batch_with_ai(batch_leads, attempt=1):
    batch_data = [
        {
            "index":   i,
            "domain":  l.get("Domain", ""),
            "title":   clean_field(l.get("Title",""), 150),
            "meta":    clean_field(l.get("Meta_Description",""), 300),
            "content": extract_content(l.get("Page_Text",""), l.get("Domain",""))
        }
        for i, l in enumerate(batch_leads)
    ]

    user_prompt = f"Websites:\n{json.dumps(batch_data, indent=2)}"

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.05,
        "max_tokens": 3500,
        "response_format": {"type": "json_object"}
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(GROQ_API_URL, json=payload, headers=headers, timeout=90)

        if response.status_code == 429:
            global consecutive_rate_limits
            consecutive_rate_limits += 1
            stats["rate_limit_hits"] += 1
            retry_after = int(response.headers.get("Retry-After", 15))
            print(f"   ⏳ Rate limited by Groq — waiting {retry_after}s... (consecutive: {consecutive_rate_limits})")
            time.sleep(retry_after)
            return "RATE_LIMITED"

        # ⬇️ NAYA: status non-200 hote hi pehle poora error body dikhao, phir raise karo
        if response.status_code != 200:
            print(f"   ❌ Groq API HTTP error: status={response.status_code}")
            print(f"   ↳ URL: {response.url}")
            print(f"   ↳ Body: {response.text[:500]}")
            response.raise_for_status()

        raw = response.json()["choices"][0]["message"]["content"].strip()

        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:-1])
            if raw.startswith("json"):
                raw = raw[4:].strip()

        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            parsed = parsed.get("items") or next((v for v in parsed.values() if isinstance(v, list)), [parsed])
        if not isinstance(parsed, list):
            return "JSON_ERROR"

        domain_map = {}
        index_map  = {}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            biz  = item.get("true_business_type", "")
            prod = item.get("true_product_category", "")
            dom  = item.get("domain", "")
            result = {
                "pitch": normalize_category(item.get("pitch_category",""), dom, biz, prod),
                "biz":   biz if biz and biz != "Unknown" else "",
                "prod":  prod if prod and prod != "Unknown" else ""
            }
            if dom:
                domain_map[dom] = result
            if item.get("index") is not None:
                index_map[int(item["index"])] = result

        return {"domain_map": domain_map, "index_map": index_map}

    except requests.exceptions.ConnectionError:
        return "CONNECTION_ERROR"
    except requests.exceptions.Timeout:
        print(f"   ⏱️ Timeout (attempt {attempt})")
        return "TIMEOUT"
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"   ⚠️ JSON error (attempt {attempt}): {str(e)[:80]}")
        try:
            print(f"   ↳ Raw response was: {response.text[:500]}")
        except Exception:
            pass
        return "JSON_ERROR"
    except requests.exceptions.HTTPError as e:
        # Body already printed above before raise_for_status(), so just log the short summary here.
        print(f"   ❌ Groq API HTTPError raised: {str(e)[:150]}")
        return "UNKNOWN_ERROR"
    except Exception as e:
        print(f"   ❌ Error: {str(e)[:100]}")
        return "UNKNOWN_ERROR"

def categorize_single_lead_fallback(lead):
    """Jab poora batch ka JSON invalid ho jaaye, ek lead ko akela bhejo —
    chhota prompt = kam chance of malformed JSON. Sirf ek attempt, koi loop nahi."""
    result = categorize_batch_with_ai([lead], attempt=1)
    if isinstance(result, dict) and "domain_map" in result:
        return get_ai_result(result, lead.get("Domain", ""), 0)
    return None

# ==========================================
# 🔁 RETRY WRAPPER
# ==========================================
DEFAULT = {"pitch": "🟢 6. General Contacts", "biz": "Review Manually", "prod": "Review Manually"}

def get_ai_result(mapping, domain, index):
    if mapping is None:
        return DEFAULT
    if domain in mapping["domain_map"]:
        return mapping["domain_map"][domain]
    if index in mapping["index_map"]:
        return mapping["index_map"][index]
    return DEFAULT

def process_batch_with_retry(batch, batch_num):
    global consecutive_rate_limits
    real_attempt = 0
    rate_limit_attempt = 0

    while real_attempt < MAX_RETRIES and rate_limit_attempt < MAX_RATE_LIMIT_RETRIES:
        result = categorize_batch_with_ai(batch, real_attempt + 1)

        if isinstance(result, dict) and "domain_map" in result:
            consecutive_rate_limits = 0  # success — reset the adaptive backoff
            return result

        elif result == "RATE_LIMITED":
            # Rate limit is not a real failure — retry with its own budget,
            # doesn't eat into MAX_RETRIES meant for genuine errors.
            rate_limit_attempt += 1
            stats["retries"] += 1
            continue

        elif result == "CONNECTION_ERROR":
            real_attempt += 1
            stats["retries"] += 1
            print("🛑 Groq se connect nahi ho paaya! 15s wait...")
            time.sleep(15)

        else:
            real_attempt += 1
            stats["retries"] += 1
            if real_attempt < MAX_RETRIES:
                wait = 5 * real_attempt
                print(f"   🔁 Retry {real_attempt}/{MAX_RETRIES} in {wait}s...")
                time.sleep(wait)

    # ⬇️ NAYA: poora batch skip karne se pehle, ek-ek lead alag se try karo.
    # JSON-validate-fail jaisa error aksar batch-size ki wajah se hota hai —
    # single-lead request me chance kam hota hai.
    print(f"   🧩 Batch {batch_num} ke leads ko individually try kar rahe hain (last resort)...")
    domain_map, index_map = {}, {}
    saved = 0
    for i, lead in enumerate(batch):
        single_result = categorize_single_lead_fallback(lead)
        if single_result:
            domain_map[lead.get("Domain", "")] = single_result
            index_map[i] = single_result
            saved += 1
        time.sleep(0.3)

    if saved > 0:
        stats["json_fallback_saved"] += saved
        print(f"   ✅ {saved}/{len(batch)} leads fallback se bach gaye.")

    if saved < len(batch):
        stats["ai_skipped"] += (len(batch) - saved)

    if saved == 0:
        print(f"   🚫 Batch {batch_num} fully skip.")
        return None

    return {"domain_map": domain_map, "index_map": index_map}

# ==========================================
# 🚀 MAIN
# ==========================================
def main():
    print("=" * 60)
    print("☁️  [GROQ CLOUD AI] Smart Categorizer — Bawa God Mode")
    print("=" * 60)

    if not GROQ_API_KEY:
        print("❌ GROQ_API_KEY set nahi hai! GitHub repo secrets me add karo ya env var set karo.")
        sys.exit(1)

    if not os.path.exists(INPUT_FILE):
        print(f"❌ '{INPUT_FILE}' nahi mila!")
        sys.exit(1)

    # ⬇️ NAYA: run se pehle hi Groq key + model + endpoint sab check ho jayega
    run_preflight_check()

    all_leads = []
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        original_fieldnames = reader.fieldnames or []
        for row in reader:
            all_leads.append(row)

    extra = ["Pitch_Category", "Business_Type", "Product_Category"]
    fieldnames = extra + [fn for fn in original_fieldnames if fn not in extra]

    processed_domains = set()
    file_mode = "w"
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                processed_domains.add(row.get("Domain", ""))
        file_mode = "a"
        print(f"⏭️  Resuming — {len(processed_domains)} leads already done.")

    leads_to_process = [l for l in all_leads if l.get("Domain") not in processed_domains]
    total = len(leads_to_process)
    if total == 0:
        print("✅ Saari leads ho chuki hain!")
        return

    prelaunch = [l for l in leads_to_process if is_prelaunch(l) and not is_parked(l)]
    parked    = [l for l in leads_to_process if is_parked(l)]
    no_data   = [l for l in leads_to_process if not is_prelaunch(l) and not is_parked(l) and has_no_content(l)]
    ai_leads  = [l for l in leads_to_process if not is_prelaunch(l) and not is_parked(l) and not has_no_content(l)]
    total_batches = (len(ai_leads) + BATCH_SIZE - 1) // BATCH_SIZE

    print(f"📊 Total pending   : {total}")
    print(f"🔥 Pre-Launch      : {len(prelaunch)}")
    print(f"🅿️  Parked          : {len(parked)}")
    print(f"⬛ No content       : {len(no_data)}")
    print(f"🤖 AI to process   : {len(ai_leads)}  (model: {MODEL_NAME})")
    print("-" * 60 + "\n")

    start_time = time.time()

    with open(OUTPUT_FILE, file_mode, newline="", encoding="utf-8") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=fieldnames, extrasaction="ignore")
        if file_mode == "w":
            writer.writeheader()

        for lead in prelaunch:
            lead["Pitch_Category"]   = "🔥 1. Pre-Launch"
            lead["Business_Type"]    = "Pre-Launch Brand"
            lead["Product_Category"] = "Coming Soon"
            writer.writerow(lead)
            stats["auto_done"] += 1

        for lead in parked:
            lead["Pitch_Category"]   = "🟢 6. General Contacts"
            lead["Business_Type"]    = "Parked / No Website"
            lead["Product_Category"] = "N/A"
            writer.writerow(lead)
            stats["auto_done"] += 1

        for lead in no_data:
            lead["Pitch_Category"]   = "🟢 6. General Contacts"
            lead["Business_Type"]    = "No Content Found"
            lead["Product_Category"] = "N/A"
            writer.writerow(lead)
            stats["auto_done"] += 1

        out_f.flush()
        if stats["auto_done"]:
            print(f"⚡ {stats['auto_done']} instant leads done.\n")

        for batch_idx in range(0, len(ai_leads), BATCH_SIZE):
            batch     = ai_leads[batch_idx:batch_idx + BATCH_SIZE]
            batch_num = batch_idx // BATCH_SIZE + 1
            pct       = (batch_idx / len(ai_leads) * 100) if ai_leads else 100

            print(f"🤖 Batch {batch_num}/{total_batches}  |  {pct:.1f}%  |  {len(batch)} leads")

            mapping = process_batch_with_retry(batch, batch_num)

            for i, lead in enumerate(batch):
                domain  = lead.get("Domain", "")
                ai_data = get_ai_result(mapping, domain, i)
                lead["Pitch_Category"]   = ai_data["pitch"]
                lead["Business_Type"]    = ai_data["biz"] or "Review Manually"
                lead["Product_Category"] = ai_data["prod"] or "Review Manually"
                writer.writerow(lead)
                stats["processed"] += 1

            out_f.flush()
            elapsed = time.time() - start_time
            eta     = int((elapsed / batch_num) * (total_batches - batch_num))
            print(f"   ✅ ETA: ~{eta//60}m {eta%60}s | Skipped: {stats['ai_skipped']} | Retries: {stats['retries']} | RateLimits: {stats['rate_limit_hits']}")

            # ⬇️ NAYA: adaptive spacing — agar rate-limits lagatar lag rahe hain,
            # to batches ke beech ka gap khud badhao (aur kabhi kam bhi karo jab sab smooth ho).
            global current_batch_sleep
            if consecutive_rate_limits >= 2:
                current_batch_sleep = min(current_batch_sleep * 1.5, MAX_BATCH_SLEEP)
            elif consecutive_rate_limits == 0 and current_batch_sleep > BASE_BATCH_SLEEP:
                current_batch_sleep = max(current_batch_sleep * 0.9, BASE_BATCH_SLEEP)
            time.sleep(current_batch_sleep)

    total_time = int(time.time() - start_time)
    print("\n" + "=" * 60)
    print("🎉 COMPLETE!")
    print("=" * 60)
    print(f"⚡ Instant       : {stats['auto_done']}")
    print(f"🤖 AI done       : {stats['processed']}")
    print(f"🧩 JSON fallback : {stats['json_fallback_saved']} (saved via single-lead retry)")
    print(f"🚫 Skipped       : {stats['ai_skipped']}")
    print(f"🔁 Real retries  : {stats['retries']}")
    print(f"⏳ Rate limits   : {stats['rate_limit_hits']}")
    print(f"⏱️  Time          : {total_time // 60}m {total_time % 60}s")
    print("=" * 60)

if __name__ == "__main__":
    main()
