import argparse
import json
import os
import shutil
import time
import csv
from datetime import datetime

import requests

from ai_queue_manager import (
    BASE_DIR,
    claim_batch,
    cleanup_expired_claims,
    publish_result_checkpoint,
    publish_result_and_release,
    write_claim_input,
)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", 300))
CLAIM_SIZE = int(os.environ.get("OLLAMA_CLAIM_SIZE", 50))
CHECKPOINT_EVERY = int(os.environ.get("OLLAMA_CHECKPOINT_EVERY", 10))
POLL_SECONDS = int(os.environ.get("OLLAMA_POLL_SECONDS", 60))

SYSTEM_PROMPT = """You are an expert business intelligence classifier.
Classify the website into exactly one outreach category using domain, title, meta description, page content, and intent clues.
Do not invent facts. Return concise niche, true_business_type, and true_product_category.

Categories:
1 = Pre-Launch: clearly coming soon, launching, waitlist, beta, early access.
2 = SaaS/Tech: software, SaaS, AI products, APIs, platforms, developer tools, automation, cloud/technical products.
3 = D2C Ad Spenders: physical-product brands selling products online; shop, cart, checkout, shipping, collections, buy now.
4 = Video-First Brands: media, production, YouTube-first businesses, creators, content studios, podcasts, film/video companies.
5 = Service Agencies: agencies, consultants, clinics/doctors, restaurants, schools, law firms, local businesses, marketing/professional services.
6 = General Contacts: use only when strong business signals are absent.
"""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "pitch_id": {"type": "integer", "minimum": 1, "maximum": 6},
        "niche": {"type": "string"},
        "true_business_type": {"type": "string"},
        "true_product_category": {"type": "string"},
    },
    "required": ["pitch_id", "niche", "true_business_type", "true_product_category"],
}

PITCHES = {
    1: "🔥 1. Pre-Launch",
    2: "🤖 2. SaaS/Tech",
    3: "💰 3. D2C Ad Spenders",
    4: "🎬 4. Video-First Brands",
    5: "🛠️ 5. Service Agencies",
    6: "🟢 6. General Contacts",
}


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def clean(value, n=250):
    value = "" if value is None else str(value).strip()
    return value[:n]


def build_context(row):
    return {
        "domain": clean(row.get("Domain"), 120),
        "title": clean(row.get("Title"), 200),
        "meta": clean(row.get("Meta_Description"), 400),
        "content": clean(row.get("Page_Text"), 1200),
        "brand_stage": clean(row.get("Brand_Stage"), 100),
        "business_type_hint": clean(row.get("Business_Type"), 150),
        "product_category_hint": clean(row.get("Product_Category"), 200),
        "intent": clean(row.get("Intent_Trust"), 150),
    }


def call_ollama(row):
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Classify this website and return only the requested JSON fields.\n"
                + json.dumps(build_context(row), ensure_ascii=False),
            },
        ],
    }

    response = requests.post(
        OLLAMA_URL.rstrip("/") + "/api/chat",
        json=payload,
        timeout=OLLAMA_TIMEOUT,
    )
    response.raise_for_status()
    outer = response.json()
    content = ((outer.get("message") or {}).get("content") or "").strip()
    if not content:
        raise ValueError("Ollama returned empty content")
    parsed = json.loads(content)
    pitch_id = int(parsed.get("pitch_id"))
    if pitch_id not in PITCHES:
        raise ValueError("Invalid pitch_id")
    niche = clean(parsed.get("niche"), 100) or "General"
    biz = clean(parsed.get("true_business_type"), 160) or "Review Manually"
    prod = clean(parsed.get("true_product_category"), 200) or "Review Manually"

    result = dict(row)
    result["Pitch_Category"] = f"{PITCHES[pitch_id]} ({niche})"
    result["Business_Type"] = biz
    result["Product_Category"] = prod
    return result


def run_once():
    cleanup_expired_claims()
    claim = claim_batch("ollama", CLAIM_SIZE)
    if not claim:
        return 0

    input_file = write_claim_input(claim)
    work_dir = claim["work_dir"]
    result_file = os.path.join(work_dir, "ollama_results.csv")

    completed = []
    fields = []
    processed_since_checkpoint = 0
    total = len(claim["rows"])
    log(f"🦙 OLLAMA WORKER: claimed {total} leads (claim={claim['claim_id']}, model={OLLAMA_MODEL})")

    try:
        for idx, row in enumerate(claim["rows"], 1):
            domain = row.get("Domain", "")
            try:
                result = call_ollama(row)
                completed.append(result)
                for field in result.keys():
                    if field not in fields:
                        fields.append(field)
                log(f"🦙 {idx}/{total} ✅ {domain}")
            except Exception as exc:
                log(f"🦙 {idx}/{total} ⚠️ {domain}: {exc}")

            processed_since_checkpoint += 1
            if completed and processed_since_checkpoint >= CHECKPOINT_EVERY:
                with open(result_file, "w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(completed)
                publish_result_checkpoint(claim, result_file)
                processed_since_checkpoint = 0

        # Final publish + release. If some leads failed, their claims are still
        # released so another worker can claim them on the next pass.
        if completed:
            with open(result_file, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(completed)
            count = publish_result_and_release(claim, result_file)
        else:
            count = publish_result_and_release(claim, result_file)

        log(f"✅ OLLAMA WORKER finished claim={claim['claim_id']}; published={count}")
        return count
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="LeadRadar local Ollama queue worker")
    parser.add_argument("--once", action="store_true", help="Process one claim and exit")
    parser.add_argument("--poll-seconds", type=int, default=POLL_SECONDS)
    args = parser.parse_args()

    log(f"🦙 Ollama worker online: {OLLAMA_URL} | model={OLLAMA_MODEL}")
    try:
        requests.get(OLLAMA_URL.rstrip("/") + "/api/tags", timeout=10).raise_for_status()
    except Exception as exc:
        log(f"❌ Ollama server not reachable: {exc}")
        log(f"   Start Ollama and ensure model is installed: ollama pull {OLLAMA_MODEL}")
        raise SystemExit(1)

    while True:
        processed = run_once()
        if args.once or processed == 0:
            if processed == 0:
                log("ℹ️ No immediately available AI work. Exiting.")
            break
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
