import csv
import json
import os
import re
import sys
import time
import random
import shutil
import requests


# ============================================================
# BAWA AI LEAD CATEGORIZER v3.1 (FIXED)
# ------------------------------------------------------------
# INPUT:
#   Ultimate_God_Leads.csv
#
# FINAL OUTPUT:
#   Bawa_Categorized_Leads.csv
#
# PARTIAL OUTPUT:
#   Bawa_Categorized_Leads.partial.csv
#
# PURPOSE:
#   Website X-Ray data ko AI se classify karke final lead
#   categories generate karna.
#
# v3.1 FIX (over v3.0):
#   get_retry_after() used to clamp Groq's Retry-After header to
#   a max of 120s. If Groq ever returns a 429 because of a HARD
#   quota wall (e.g. daily token quota exhausted, real
#   Retry-After could be hours), the old code would silently
#   treat it as "wait 120s" and burn through MAX_RATE_LIMIT_RETRIES
#   (8) short waits — then still fall into the single-lead
#   fallback loop and hit the same wall again per lead. That
#   wastes a lot of run time for something retries can't fix.
#   Now: if Retry-After is large (> RATE_LIMIT_HARD_WALL_SECONDS),
#   we treat it as a hard quota wall, log it clearly, and bail
#   out of BOTH batch retries and single-lead fallback for this
#   run immediately — leads stay safely pending for the next run
#   instead of the script spinning uselessly.
# ============================================================


# ============================================================
# CONFIGURATION
# ============================================================

INPUT_FILE = "Ultimate_God_Leads.csv"

OUTPUT_FILE = "Bawa_Categorized_Leads.csv"

PARTIAL_OUTPUT_FILE = (
    "Bawa_Categorized_Leads.partial.csv"
)

GROQ_API_URL = (
    "https://api.groq.com/openai/v1/chat/completions"
)

GROQ_MODELS_URL = (
    "https://api.groq.com/openai/v1/models"
)

GROQ_API_KEY = os.environ.get(
    "GROQ_API_KEY",
    ""
)

MODEL_NAME = os.environ.get(
    "GROQ_MODEL",
    "openai/gpt-oss-20b"
)


# ------------------------------------------------------------
# Batch settings
# ------------------------------------------------------------

BATCH_SIZE = int(
    os.environ.get(
        "GROQ_BATCH_SIZE",
        "5"
    )
)

MAX_RETRIES = 3

MAX_RATE_LIMIT_RETRIES = 8

SINGLE_LEAD_FALLBACK = True

# If Groq's Retry-After is bigger than this, it's almost
# certainly a hard quota wall (daily/monthly limit), not a
# short burst limit — retrying won't help within this run.
RATE_LIMIT_HARD_WALL_SECONDS = 300


# ------------------------------------------------------------
# Timing
# ------------------------------------------------------------

BASE_BATCH_SLEEP = 0.5

MAX_BATCH_SLEEP = 12.0

current_batch_sleep = (
    BASE_BATCH_SLEEP
)

consecutive_rate_limits = 0

# Set once we detect a hard quota wall so the rest of this run
# can stop attempting AI calls entirely instead of retrying
# batch after batch into the same wall.
quota_exhausted = False


# ------------------------------------------------------------
# Request timeout
# ------------------------------------------------------------

API_TIMEOUT = 120


# ============================================================
# STATISTICS
# ============================================================

stats = {
    "processed": 0,
    "auto_done": 0,
    "ai_done": 0,
    "pending": 0,
    "retries": 0,
    "rate_limit_hits": 0,
    "single_fallback_attempts": 0,
    "single_fallback_saved": 0,
    "validation_failures": 0,
}


# ============================================================
# OUTPUT COLUMNS
# ------------------------------------------------------------
# Existing downstream contract preserved.
# ============================================================

OUTPUT_COLUMNS = [
    "Pitch_Category",
    "Business_Type",
    "Product_Category",
]


# ============================================================
# NAVIGATION / BOILERPLATE WORDS
# ============================================================

NAV_WORDS = {
    "home",
    "about",
    "contact",
    "menu",
    "toggle",
    "navigation",
    "nav",
    "skip",
    "close",
    "open",
    "search",
    "cart",
    "login",
    "register",
    "signup",
    "sign",
    "next",
    "previous",
    "back",
    "read",
    "more",
    "click",
    "here",
    "cookie",
    "privacy",
    "policy",
    "terms",
    "copyright",
    "all",
    "rights",
    "reserved",
    "powered",
    "inc",
    "llc",
    "ltd",
    "password",
    "enter",
    "get",
    "started",
    "learn",
}


# ============================================================
# CATEGORY DEFINITIONS
# ============================================================

PITCH_CATEGORIES = {
    1: "🔥 1. Pre-Launch",
    2: "🤖 2. SaaS/Tech",
    3: "💰 3. D2C Ad Spenders",
    4: "🎬 4. Video-First Brands",
    5: "🛠️ 5. Service Agencies",
    6: "🟢 6. General Contacts",
}


# ============================================================
# BASIC TEXT CLEANING
# ============================================================

def clean_field(
    text,
    max_len=300,
):
    if not text:
        return ""

    text = str(text)

    if text.strip().lower() in {
        "none",
        "null",
        "nan",
    }:
        return ""

    text = (
        text
        .encode(
            "utf-8",
            errors="ignore",
        )
        .decode(
            "utf-8",
            errors="ignore",
        )
    )

    text = "".join(
        char
        for char in text
        if char.isprintable()
        or char in "\n\t"
    )

    text = (
        text
        .replace("\\", " ")
        .replace('"', "'")
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()[:max_len]


def normalize_domain(
    domain,
):
    if not domain:
        return ""

    domain = (
        str(domain)
        .strip()
        .lower()
    )

    domain = re.sub(
        r"^[a-z][a-z0-9+.-]*://",
        "",
        domain,
        flags=re.I,
    )

    domain = re.sub(
        r"^www\.",
        "",
        domain,
        flags=re.I,
    )

    domain = re.split(
        r"[/?#]",
        domain,
        maxsplit=1,
    )[0]

    return domain.rstrip(".")


# ============================================================
# AI CONTENT EXTRACTION
# ============================================================

def extract_content(
    text,
    domain="",
    max_words=90,
):
    """
    Remove obvious navigation garbage and duplicate words.

    Keeps enough semantic content for AI classification without
    unnecessarily increasing token usage.
    """

    if not text:
        return ""

    text = clean_field(
        text,
        1200,
    )

    if not text:
        return ""

    domain_root = re.sub(
        r"[^a-z0-9]",
        "",
        normalize_domain(
            domain
        ).split(".")[0],
    )

    words = re.findall(
        r"[A-Za-z]{3,}",
        text,
    )

    seen = {}

    clean = []

    for word in words:

        word_lower = word.lower()

        if word_lower in NAV_WORDS:
            continue

        if (
            domain_root
            and word_lower == domain_root
        ):
            continue

        seen[word_lower] = (
            seen.get(
                word_lower,
                0
            )
            + 1
        )

        # Avoid repetitive website boilerplate.
        if seen[word_lower] > 2:
            continue

        clean.append(
            word
        )

        if len(clean) >= max_words:
            break

    return " ".join(
        clean
    )


# ============================================================
# INSTANT SIGNALS
# ============================================================

PARKED_SIGNALS = [
    "parked domain",
    "hostinger dns",
    "domain for sale",
    "this domain is for sale",
    "buy this domain",
    "hugedomains",
    "sedoparking",
    "undeveloped",
    "domain parking",
    "welcome to nginx",
]


PRELAUNCH_SIGNALS = [
    "launching soon",
    "coming soon",
    "under construction",
    "check back for an update",
    "check back soon",
    "being worked on",
    "we're under construction",
    "opening soon",
    "be the first to know when we launch",
    "join the waitlist",
    "early access",
]


def lead_text(
    lead,
):
    return (
        clean_field(
            lead.get(
                "Title",
                ""
            ),
            200,
        )
        + " "
        + clean_field(
            lead.get(
                "Meta_Description",
                ""
            ),
            400,
        )
        + " "
        + clean_field(
            lead.get(
                "Page_Text",
                ""
            ),
            800,
        )
    ).lower()


def is_parked(
    lead,
):
    text = lead_text(
        lead
    )

    return any(
        signal in text
        for signal in PARKED_SIGNALS
    )


def is_prelaunch(
    lead,
):
    stage = (
        clean_field(
            lead.get(
                "Brand_Stage",
                ""
            ),
            200,
        )
        .lower()
    )

    text = lead_text(
        lead
    )

    if "pre-launch" in stage:
        return True

    return any(
        signal in text
        for signal in PRELAUNCH_SIGNALS
    )


def has_no_content(
    lead,
):
    title = clean_field(
        lead.get(
            "Title",
            ""
        ),
        200,
    )

    meta = clean_field(
        lead.get(
            "Meta_Description",
            ""
        ),
        300,
    )

    content = extract_content(
        lead.get(
            "Page_Text",
            ""
        ),
        lead.get(
            "Domain",
            ""
        ),
    )

    combined = (
        title
        + meta
        + content
    )

    return len(
        combined.strip()
    ) < 10


# ============================================================
# NICHE RESOLUTION
# ============================================================

def infer_niche_from_text(
    text,
):
    """
    Local deterministic fallback for cases where AI gives
    no useful niche.
    """

    text = (
        text or ""
    ).lower()

    niche_rules = [
        (
            "Healthcare",
            [
                "health",
                "medical",
                "clinic",
                "doctor",
                "pharma",
                "dental",
                "hospital",
                "therapy",
            ],
        ),
        (
            "Fitness",
            [
                "fitness",
                "gym",
                "sport",
                "athlet",
                "workout",
                "wellness",
            ],
        ),
        (
            "Fashion",
            [
                "fashion",
                "cloth",
                "wear",
                "apparel",
                "textile",
                "outfit",
            ],
        ),
        (
            "Food & Beverage",
            [
                "food",
                "restaurant",
                "cafe",
                "dining",
                "kitchen",
                "catering",
                "coffee",
                "bakery",
            ],
        ),
        (
            "Tech",
            [
                "tech",
                "software",
                "saas",
                "ai",
                "digital",
                "cloud",
                "platform",
            ],
        ),
        (
            "Real Estate",
            [
                "real estate",
                "property",
                "realty",
                "housing",
                "apartment",
                "villa",
            ],
        ),
        (
            "Marketing",
            [
                "marketing",
                "agency",
                "seo",
                "ads",
                "creative",
                "social media",
            ],
        ),
        (
            "Education",
            [
                "education",
                "school",
                "learn",
                "tutor",
                "academy",
                "course",
                "training",
            ],
        ),
        (
            "Beauty",
            [
                "beauty",
                "skin",
                "cosmetic",
                "salon",
                "hair",
                "makeup",
            ],
        ),
        (
            "Finance",
            [
                "finance",
                "invest",
                "wealth",
                "banking",
                "insurance",
                "fintech",
                "crypto",
            ],
        ),
        (
            "Travel",
            [
                "travel",
                "hotel",
                "resort",
                "holiday",
                "tour",
                "hospitality",
            ],
        ),
    ]

    for niche, signals in niche_rules:

        if any(
            signal in text
            for signal in signals
        ):
            return niche

    return "General"


# ============================================================
# PITCH NORMALIZATION
# ============================================================

def build_pitch_category(
    pitch_id,
    niche,
    lead_text_value="",
):
    """
    AI returns a numeric pitch_id.
    We construct the final canonical string ourselves.
    """

    try:
        pitch_id = int(
            pitch_id
        )
    except (
        TypeError,
        ValueError,
    ):
        pitch_id = 6

    if pitch_id not in PITCH_CATEGORIES:
        pitch_id = 6

    if pitch_id == 1:
        return PITCH_CATEGORIES[1]

    niche = clean_field(
        niche,
        80,
    )

    if not niche:
        niche = infer_niche_from_text(
            lead_text_value
        )

    # Remove placeholder AI outputs.
    if niche.lower() in {
        "niche",
        "unknown",
        "general",
        "general / other",
        "n/a",
    }:

        niche = infer_niche_from_text(
            lead_text_value
        )

    return (
        f"{PITCH_CATEGORIES[pitch_id]} "
        f"({niche})"
    )


# ============================================================
# AI JSON SCHEMA
# ============================================================

AI_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "items": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "index": {
                        "type": "integer",
                        "minimum": 0,
                    },
                    "domain": {
                        "type": "string",
                    },
                    "pitch_id": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 6,
                    },
                    "niche": {
                        "type": "string",
                    },
                    "true_business_type": {
                        "type": "string",
                    },
                    "true_product_category": {
                        "type": "string",
                    },
                },
                "required": [
                    "index",
                    "domain",
                    "pitch_id",
                    "niche",
                    "true_business_type",
                    "true_product_category",
                ],
            },
        },
    },
    "required": [
        "items",
    ],
}


# ============================================================
# AI SYSTEM PROMPT
# ============================================================

SYSTEM_PROMPT = """
You are an expert business intelligence classifier.

Your job is to classify websites into one of six business
outreach categories.

USE ALL AVAILABLE SIGNALS:
- domain
- title
- meta description
- page content
- website intent clues

IMPORTANT:
Do NOT invent a business based only on a generic domain unless
there are no other signals.

PITCH CATEGORIES:

1 = Pre-Launch
Use when the company/site is clearly coming soon,
launching soon, waitlist, early access, beta launch, etc.

2 = SaaS/Tech
Use for:
software
SaaS
AI products
APIs
platforms
developer tools
automation
cloud products
technical products

3 = D2C Ad Spenders
Use for physical-product brands that sell products online.
Signals include:
add to cart
shop
checkout
free shipping
product collection
buy now
new arrivals
etc.

4 = Video-First Brands
Use for:
media companies
production studios
YouTube-first businesses
video creators
content studios
podcasts
film/video companies

5 = Service Agencies
Use for:
agencies
consultants
clinics/doctors
restaurants
schools
law firms
local businesses
marketing services
professional services

6 = General Contacts
Use ONLY when strong business signals are absent.

NICHE:
Return a concise real industry/niche.

Examples:
Beauty
Organic Fashion
AI Robotics
Personality Analytics
Healthcare
Influencer Marketing
Real Estate
SaaS
Travel
Education

TRUE BUSINESS TYPE:
Return a specific business type where evidence exists.

Examples:
Orthopaedic Clinic
D2C Skincare Brand
AI Workflow SaaS
Digital Marketing Agency
Luxury Travel Company

TRUE PRODUCT CATEGORY:
Return the actual product/service category.

Examples:
Joint Replacement Surgery
Skincare Products
CRM Automation Software
Influencer Marketing
Luxury Holiday Packages

DO NOT output:
Unknown
N/A
NICHE
Generic filler unless the evidence is genuinely absent.

Return one result for EVERY input item.
Keep the same index as the input.
"""


# ============================================================
# API HEADERS
# ============================================================

def api_headers():
    return {
        "Authorization": (
            f"Bearer {GROQ_API_KEY}"
        ),
        "Content-Type": (
            "application/json"
        ),
    }


# ============================================================
# SAFE RETRY-AFTER PARSER
# ------------------------------------------------------------
# v3.1 FIX: no longer clamps to a tiny 120s ceiling. We need the
# real value to tell a short burst-limit apart from a long hard
# quota wall (see RATE_LIMIT_HARD_WALL_SECONDS usage below). We
# still cap at 24h just to guard against a garbage header value
# causing an absurd sleep somewhere else in the code.
# ============================================================

def get_retry_after(
    response,
):
    value = response.headers.get(
        "Retry-After"
    )

    if value is None:
        return 15

    try:
        return max(
            1,
            min(
                int(float(value)),
                86400,
            )
        )
    except (
        TypeError,
        ValueError,
    ):
        return 15


# ============================================================
# PRE-FLIGHT
# ============================================================

def run_preflight_check():

    print(
        "🩺 Groq preflight check..."
    )

    if not GROQ_API_KEY:

        print(
            "❌ GROQ_API_KEY set nahi hai."
        )

        return False

    headers = api_headers()

    # --------------------------------------------------------
    # Model list
    # --------------------------------------------------------

    try:

        response = requests.get(
            GROQ_MODELS_URL,
            headers=headers,
            timeout=30,
        )

    except requests.exceptions.RequestException as exc:

        print(
            f"❌ Groq models endpoint failed: "
            f"{exc}"
        )

        return False

    if response.status_code == 401:

        print(
            "❌ GROQ_API_KEY invalid/expired."
        )

        return False

    if response.status_code != 200:

        print(
            f"❌ Models endpoint HTTP "
            f"{response.status_code}"
        )

        print(
            response.text[:500]
        )

        return False

    try:

        data = response.json()

        available_models = {
            item.get("id")
            for item in data.get(
                "data",
                []
            )
            if item.get("id")
        }

    except Exception as exc:

        print(
            f"❌ Model response parse failed: "
            f"{exc}"
        )

        return False

    print(
        f"   ✅ API authenticated. "
        f"{len(available_models)} models visible."
    )

    if (
        available_models
        and MODEL_NAME
        not in available_models
    ):

        print(
            f"❌ Model '{MODEL_NAME}' "
            f"available nahi hai."
        )

        print(
            "   Set GROQ_MODEL to a valid active model."
        )

        return False

    print(
        f"   ✅ Model: {MODEL_NAME}"
    )

    return True


# ============================================================
# BUILD AI INPUT
# ============================================================

def build_batch_payload(
    batch,
):

    payload = []

    for index, lead in enumerate(
        batch
    ):

        domain = normalize_domain(
            lead.get(
                "Domain",
                ""
            )
        )

        title = clean_field(
            lead.get(
                "Title",
                ""
            ),
            150,
        )

        meta = clean_field(
            lead.get(
                "Meta_Description",
                ""
            ),
            300,
        )

        content = extract_content(
            lead.get(
                "Page_Text",
                ""
            ),
            domain,
            max_words=90,
        )

        payload.append(
            {
                "index": index,
                "domain": domain,
                "title": title,
                "meta": meta,
                "content": content,
            }
        )

    return payload


# ============================================================
# AI RESPONSE VALIDATION
# ============================================================

def validate_ai_items(
    parsed,
    batch,
):
    """
    Only accept AI results that can be reliably mapped to the
    original batch.

    Missing items are NOT replaced with fake defaults.
    """

    if not isinstance(
        parsed,
        dict
    ):
        return None

    items = parsed.get(
        "items"
    )

    if not isinstance(
        items,
        list
    ):
        return None

    expected_indices = set(
        range(
            len(batch)
        )
    )

    valid = {}

    for item in items:

        if not isinstance(
            item,
            dict
        ):
            continue

        try:
            index = int(
                item.get(
                    "index"
                )
            )
        except (
            TypeError,
            ValueError,
        ):
            continue

        if index not in expected_indices:
            continue

        domain = normalize_domain(
            item.get(
                "domain",
                ""
            )
        )

        expected_domain = normalize_domain(
            batch[index].get(
                "Domain",
                ""
            )
        )

        # Domain must match if returned.
        if domain and domain != expected_domain:
            continue

        try:

            pitch_id = int(
                item.get(
                    "pitch_id"
                )
            )

        except (
            TypeError,
            ValueError,
        ):

            continue

        if not (
            1
            <= pitch_id
            <= 6
        ):
            continue

        niche = clean_field(
            item.get(
                "niche",
                ""
            ),
            100,
        )

        biz = clean_field(
            item.get(
                "true_business_type",
                ""
            ),
            150,
        )

        prod = clean_field(
            item.get(
                "true_product_category",
                ""
            ),
            200,
        )

        lead_signal_text = lead_text(
            batch[index]
        )

        if not biz:
            biz = "Review Manually"

        if not prod:
            prod = "Review Manually"

        pitch = build_pitch_category(
            pitch_id,
            niche,
            lead_signal_text,
        )

        valid[index] = {
            "pitch": pitch,
            "biz": biz,
            "prod": prod,
        }

    # --------------------------------------------------------
    # We need at least one valid result.
    # --------------------------------------------------------

    if not valid:
        return None

    return valid


# ============================================================
# GROQ REQUEST
# ============================================================

def categorize_batch_with_ai(
    batch,
):
    payload_data = build_batch_payload(
        batch
    )

    user_prompt = (
        "Classify the following websites.\n\n"
        + json.dumps(
            payload_data,
            ensure_ascii=False,
            indent=2,
        )
    )

    payload = {
        "model": MODEL_NAME,

        "messages": [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],

        "temperature": 0.0,

        # GPT-OSS supports reasoning_effort.
        # Low is enough for deterministic classification.
        "reasoning_effort": "low",

        "max_tokens": 2000,

        # Strict schema is preferred over old JSON mode.
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "lead_classification",
                "strict": True,
                "schema": AI_JSON_SCHEMA,
            },
        },
    }

    try:

        response = requests.post(
            GROQ_API_URL,
            json=payload,
            headers=api_headers(),
            timeout=API_TIMEOUT,
        )

    except requests.exceptions.Timeout:
        return {
            "status": "TIMEOUT",
        }

    except requests.exceptions.ConnectionError:
        return {
            "status": "CONNECTION_ERROR",
        }

    except requests.exceptions.RequestException as exc:
        return {
            "status": "REQUEST_ERROR",
            "error": str(exc),
        }

    # ========================================================
    # RATE LIMIT
    # ========================================================

    if response.status_code == 429:

        global consecutive_rate_limits

        consecutive_rate_limits += 1

        stats[
            "rate_limit_hits"
        ] += 1

        retry_after = get_retry_after(
            response
        )

        return {
            "status": "RATE_LIMITED",
            "retry_after": retry_after,
            "body": response.text[:500],
        }

    # ========================================================
    # HTTP ERROR
    # ========================================================

    if response.status_code != 200:

        return {
            "status": "HTTP_ERROR",
            "code": response.status_code,
            "body": response.text[:1000],
        }

    # ========================================================
    # RESPONSE JSON
    # ========================================================

    try:

        outer = response.json()

        content = (
            outer[
                "choices"
            ][0][
                "message"
            ][
                "content"
            ]
        )

        if not content:
            return {
                "status": "EMPTY_RESPONSE",
            }

        parsed = json.loads(
            content
        )

    except (
        json.JSONDecodeError,
        KeyError,
        IndexError,
        TypeError,
    ) as exc:

        stats[
            "validation_failures"
        ] += 1

        return {
            "status": "JSON_ERROR",
            "error": str(exc),
            "raw": response.text[:1000],
        }

    # ========================================================
    # VALIDATE
    # ========================================================

    validated = validate_ai_items(
        parsed,
        batch,
    )

    if validated is None:

        stats[
            "validation_failures"
        ] += 1

        return {
            "status": "VALIDATION_ERROR",
            "raw": str(
                parsed
            )[:1000],
        }

    return {
        "status": "SUCCESS",
        "items": validated,
    }


# ============================================================
# SINGLE LEAD FALLBACK
# ============================================================

def categorize_single_lead(
    lead,
):
    """
    Retry one problematic lead independently.

    v3.1: skipped entirely if we've already detected a hard
    quota wall this run (see quota_exhausted flag) — a single
    lead can't succeed where a batch just hit a hard wall.
    """

    global quota_exhausted

    if quota_exhausted:
        return None

    stats[
        "single_fallback_attempts"
    ] += 1

    result = categorize_batch_with_ai(
        [lead]
    )

    if result.get(
        "status"
    ) == "RATE_LIMITED":

        retry_after = result.get(
            "retry_after",
            15,
        )

        if retry_after > RATE_LIMIT_HARD_WALL_SECONDS:

            quota_exhausted = True

            print(
                f"   🛑 Quota wall hit again during single-lead "
                f"fallback (Retry-After={retry_after}s). "
                f"Stopping fallback for remaining leads."
            )

        return None

    if result.get(
        "status"
    ) != "SUCCESS":

        return None

    items = result.get(
        "items",
        {}
    )

    return items.get(
        0
    )


# ============================================================
# PROCESS BATCH WITH RETRIES
# ============================================================

def process_batch_with_retry(
    batch,
    batch_num,
):

    global current_batch_sleep
    global consecutive_rate_limits
    global quota_exhausted

    # If a previous batch this run already confirmed a hard
    # quota wall, don't even try — just leave this batch pending.
    if quota_exhausted:
        return {}

    real_attempts = 0

    rate_limit_attempts = 0

    while (
        real_attempts
        < MAX_RETRIES
        and
        rate_limit_attempts
        < MAX_RATE_LIMIT_RETRIES
    ):

        result = (
            categorize_batch_with_ai(
                batch
            )
        )

        status = result.get(
            "status"
        )

        # ----------------------------------------------------
        # Success
        # ----------------------------------------------------

        if status == "SUCCESS":

            consecutive_rate_limits = 0

            return result.get(
                "items",
                {}
            )

        # ----------------------------------------------------
        # Rate limit
        # ----------------------------------------------------

        if status == "RATE_LIMITED":

            retry_after = result.get(
                "retry_after",
                15,
            )

            # --------------------------------------------------
            # v3.1 FIX: a large Retry-After means a hard quota
            # wall (daily/monthly limit), not a short burst
            # limit. Retrying with short waits just wastes time
            # and hits the same wall again and again. Bail out
            # immediately instead of burning through
            # MAX_RATE_LIMIT_RETRIES and then the fallback loop.
            # --------------------------------------------------

            if retry_after > RATE_LIMIT_HARD_WALL_SECONDS:

                quota_exhausted = True

                print(
                    f"   🛑 Groq Retry-After={retry_after}s — "
                    f"this looks like a hard quota wall, not a "
                    f"short rate limit. Stopping AI calls for "
                    f"this run; all remaining leads stay pending "
                    f"and will be retried on the next run."
                )

                print(
                    f"   ↳ Body: "
                    f"{result.get('body', '')[:300]}"
                )

                return {}

            rate_limit_attempts += 1

            stats[
                "retries"
            ] += 1

            # Small jitter avoids repeatedly hitting the exact
            # same boundary.
            jitter = random.uniform(
                0.2,
                1.0,
            )

            wait = (
                retry_after
                + jitter
            )

            print(
                f"   ⏳ Rate limit "
                f"{rate_limit_attempts}/"
                f"{MAX_RATE_LIMIT_RETRIES} "
                f"— waiting {wait:.1f}s..."
            )

            time.sleep(
                wait
            )

            continue

        # ----------------------------------------------------
        # Other failures
        # ----------------------------------------------------

        real_attempts += 1

        stats[
            "retries"
        ] += 1

        if real_attempts < MAX_RETRIES:

            wait = min(
                5 * real_attempts,
                20,
            )

            print(
                f"   🔁 Batch retry "
                f"{real_attempts}/"
                f"{MAX_RETRIES} "
                f"in {wait}s..."
            )

            time.sleep(
                wait
            )

    # ========================================================
    # SINGLE-LEAD FALLBACK
    # ========================================================

    if not SINGLE_LEAD_FALLBACK or quota_exhausted:

        return {}

    print(
        f"   🧩 Batch {batch_num}: "
        f"individual fallback start..."
    )

    recovered = {}

    for index, lead in enumerate(
        batch
    ):

        if quota_exhausted:
            break

        result = categorize_single_lead(
            lead
        )

        if result:

            recovered[
                index
            ] = result

            stats[
                "single_fallback_saved"
            ] += 1

        # Small spacing between fallback calls.
        time.sleep(
            0.35
        )

    if recovered:

        print(
            f"   ✅ Individual fallback: "
            f"{len(recovered)}/{len(batch)} "
            f"saved."
        )

    return recovered


# ============================================================
# CSV HELPERS
# ============================================================

def get_input_fieldnames(
    leads,
):
    if not leads:
        return []

    return list(
        leads[0].keys()
    )


def build_output_fieldnames(
    original_fields,
):
    """
    Put our new classification fields first while preserving
    all X-Ray fields.
    """

    return (
        OUTPUT_COLUMNS
        + [
            field
            for field in original_fields
            if field not in OUTPUT_COLUMNS
        ]
    )


def load_csv_rows(
    filepath,
):
    if not os.path.exists(
        filepath
    ):
        return [], []

    with open(
        filepath,
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:

        reader = csv.DictReader(
            file
        )

        rows = list(
            reader
        )

        fields = (
            reader.fieldnames
            or []
        )

    return rows, fields


# ============================================================
# PARTIAL OUTPUT RESUME
# ============================================================

def load_processed_domains(
    filepath,
):
    """
    Read only successfully written rows from partial/final file.

    This makes resume deterministic.
    """

    processed = set()

    if not os.path.exists(
        filepath
    ):
        return processed

    try:

        with open(
            filepath,
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as file:

            reader = csv.DictReader(
                file
            )

            for row in reader:

                domain = normalize_domain(
                    row.get(
                        "Domain",
                        ""
                    )
                )

                if domain:
                    processed.add(
                        domain
                    )

    except Exception as exc:

        print(
            f"⚠️ Resume file read failed: "
            f"{exc}"
        )

    return processed


# ============================================================
# INITIALIZE PARTIAL FILE
# ============================================================

def ensure_partial_file(
    fieldnames,
):
    """
    Create partial output with header when needed.
    """

    if (
        os.path.exists(
            PARTIAL_OUTPUT_FILE
        )
        and
        os.path.getsize(
            PARTIAL_OUTPUT_FILE
        ) > 0
    ):
        return

    with open(
        PARTIAL_OUTPUT_FILE,
        "w",
        encoding="utf-8",
        newline="",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )

        writer.writeheader()


# ============================================================
# APPEND PARTIAL ROW
# ============================================================

def append_partial_rows(
    rows,
    fieldnames,
):
    if not rows:
        return

    ensure_partial_file(
        fieldnames
    )

    with open(
        PARTIAL_OUTPUT_FILE,
        "a",
        encoding="utf-8",
        newline="",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )

        for row in rows:
            writer.writerow(
                row
            )

        file.flush()


# ============================================================
# FINALIZE OUTPUT
# ============================================================

def finalize_output():
    """
    Only called when ALL leads have terminal results.

    Partial file becomes final atomically.
    """

    if not os.path.exists(
        PARTIAL_OUTPUT_FILE
    ):
        return False

    if os.path.getsize(
        PARTIAL_OUTPUT_FILE
    ) == 0:
        return False

    os.replace(
        PARTIAL_OUTPUT_FILE,
        OUTPUT_FILE,
    )

    return (
        os.path.exists(
            OUTPUT_FILE
        )
        and
        os.path.getsize(
            OUTPUT_FILE
        ) > 0
    )


# ============================================================
# DELETE STALE FINAL FILE
# ============================================================

def remove_stale_final_if_needed():
    """
    If there is a partial file, we are in an incomplete/resume
    state. Do not let an old final CSV trick master_controller
    into thinking this run is complete.

    This function only removes a final file when a partial file
    exists and there are still pending leads.
    """

    if not os.path.exists(
        PARTIAL_OUTPUT_FILE
    ):
        return

    # If final exists alongside a partial, final belongs to a
    # previous incomplete/failed cycle. Remove it so controller
    # won't falsely skip this stage.
    if os.path.exists(
        OUTPUT_FILE
    ):

        try:
            os.remove(
                OUTPUT_FILE
            )

            print(
                "🧹 Removed stale final categorized file; "
                "partial resume is active."
            )

        except OSError as exc:

            print(
                f"⚠️ Could not remove stale final: {exc}"
            )


# ============================================================
# MAIN
# ============================================================

def main():

    global current_batch_sleep

    print()
    print("=" * 72)
    print(
        "☁️ BAWA GROQ AI LEAD CATEGORIZER v3.1 (FIXED)"
    )
    print("=" * 72)
    print()

    # --------------------------------------------------------
    # Basic validation
    # --------------------------------------------------------

    if not GROQ_API_KEY:

        print(
            "❌ GROQ_API_KEY set nahi hai."
        )

        return 1

    if not os.path.exists(
        INPUT_FILE
    ):

        print(
            f"❌ '{INPUT_FILE}' nahi mila."
        )

        return 1

    if not run_preflight_check():

        return 1

    # --------------------------------------------------------
    # Load input
    # --------------------------------------------------------

    try:

        all_leads, original_fields = (
            load_csv_rows(
                INPUT_FILE
            )
        )

    except Exception as exc:

        print(
            f"❌ Input CSV read failed: "
            f"{exc}"
        )

        return 1

    if not all_leads:

        print(
            "⚠️ Input CSV mein koi leads nahi hain."
        )

        return 1

    fieldnames = (
        build_output_fieldnames(
            original_fields
        )
    )

    # --------------------------------------------------------
    # Normalize + deduplicate input
    # --------------------------------------------------------

    unique_leads = []

    seen_domains = set()

    for lead in all_leads:

        domain = normalize_domain(
            lead.get(
                "Domain",
                ""
            )
        )

        if not domain:
            continue

        if domain in seen_domains:
            continue

        seen_domains.add(
            domain
        )

        lead[
            "Domain"
        ] = domain

        unique_leads.append(
            lead
        )

    all_leads = unique_leads

    print(
        f"📊 Unique input leads : "
        f"{len(all_leads):,}"
    )

    # --------------------------------------------------------
    # Resume state
    # --------------------------------------------------------

    processed_domains = set()

    if os.path.exists(
        OUTPUT_FILE
    ):

        processed_domains.update(
            load_processed_domains(
                OUTPUT_FILE
            )
        )

    if os.path.exists(
        PARTIAL_OUTPUT_FILE
    ):

        partial_domains = (
            load_processed_domains(
                PARTIAL_OUTPUT_FILE
            )
        )

        processed_domains.update(
            partial_domains
        )

    leads_to_process = [
        lead
        for lead in all_leads
        if normalize_domain(
            lead.get(
                "Domain",
                ""
            )
        )
        not in processed_domains
    ]

    print(
        f"⏭️ Already completed  : "
        f"{len(processed_domains):,}"
    )

    print(
        f"🎯 Pending           : "
        f"{len(leads_to_process):,}"
    )

    # --------------------------------------------------------
    # Nothing pending
    # --------------------------------------------------------

    if not leads_to_process:

        # Existing final is already valid.
        if (
            os.path.exists(
                OUTPUT_FILE
            )
            and
            os.path.getsize(
                OUTPUT_FILE
            ) > 0
        ):

            print(
                "✅ Final categorized file already complete."
            )

            return 0

        # Partial contains everything.
        if finalize_output():

            print(
                "✅ Partial output finalized."
            )

            return 0

        print(
            "❌ Nothing pending but no valid final output."
        )

        return 1

    # --------------------------------------------------------
    # Remove stale final if partial resume exists.
    # --------------------------------------------------------

    remove_stale_final_if_needed()

    # --------------------------------------------------------
    # Ensure partial output
    # --------------------------------------------------------

    ensure_partial_file(
        fieldnames
    )

    # --------------------------------------------------------
    # Instant classification groups
    # --------------------------------------------------------

    prelaunch = []

    parked = []

    no_data = []

    ai_leads = []

    for lead in leads_to_process:

        if is_prelaunch(
            lead
        ):

            prelaunch.append(
                lead
            )

        elif is_parked(
            lead
        ):

            parked.append(
                lead
            )

        elif has_no_content(
            lead
        ):

            no_data.append(
                lead
            )

        else:

            ai_leads.append(
                lead
            )

    print()
    print(
        f"🔥 Pre-Launch : "
        f"{len(prelaunch):,}"
    )

    print(
        f"🅿️ Parked      : "
        f"{len(parked):,}"
    )

    print(
        f"⬛ No Content  : "
        f"{len(no_data):,}"
    )

    print(
        f"🤖 AI Pending  : "
        f"{len(ai_leads):,}"
    )

    print(
        "-" * 72
    )

    # ========================================================
    # INSTANT PROCESSING
    # ========================================================

    instant_rows = []

    # --------------------------------------------------------
    # Pre-launch
    # --------------------------------------------------------

    for lead in prelaunch:

        lead[
            "Pitch_Category"
        ] = "🔥 1. Pre-Launch"

        lead[
            "Business_Type"
        ] = "Pre-Launch Brand"

        lead[
            "Product_Category"
        ] = "Coming Soon"

        instant_rows.append(
            lead
        )

    # --------------------------------------------------------
    # Parked
    # --------------------------------------------------------

    for lead in parked:

        lead[
            "Pitch_Category"
        ] = "🟢 6. General Contacts"

        lead[
            "Business_Type"
        ] = "Parked / Domain For Sale"

        lead[
            "Product_Category"
        ] = "N/A"

        instant_rows.append(
            lead
        )

    # --------------------------------------------------------
    # No content
    # --------------------------------------------------------

    for lead in no_data:

        lead[
            "Pitch_Category"
        ] = "🟢 6. General Contacts"

        lead[
            "Business_Type"
        ] = "No Content Found"

        lead[
            "Product_Category"
        ] = "N/A"

        instant_rows.append(
            lead
        )

    if instant_rows:

        append_partial_rows(
            instant_rows,
            fieldnames,
        )

        stats[
            "auto_done"
        ] += len(
            instant_rows
        )

    # ========================================================
    # AI PROCESSING
    # ========================================================

    total_batches = (
        len(ai_leads)
        + BATCH_SIZE
        - 1
    ) // BATCH_SIZE

    for batch_start in range(
        0,
        len(ai_leads),
        BATCH_SIZE,
    ):

        # If a hard quota wall was already detected, stop
        # launching new batches entirely — they'll just fail the
        # same way. Remaining leads stay pending for next run.
        if quota_exhausted:

            print(
                "🛑 Quota wall active — skipping remaining "
                "batches for this run."
            )

            break

        batch = ai_leads[
            batch_start:
            batch_start + BATCH_SIZE
        ]

        batch_num = (
            batch_start
            // BATCH_SIZE
            + 1
        )

        percent = (
            batch_start
            / len(ai_leads)
            * 100
            if ai_leads
            else 100
        )

        print()
        print(
            f"🤖 Batch "
            f"{batch_num}/{total_batches} "
            f"| {percent:.1f}% "
            f"| {len(batch)} leads"
        )

        mapping = (
            process_batch_with_retry(
                batch,
                batch_num,
            )
        )

        if not mapping:
            mapping = {}

        successful_rows = []

        unresolved_count = 0

        for index, lead in enumerate(
            batch
        ):

            result = mapping.get(
                index
            )

            if result is None:

                unresolved_count += 1

                continue

            lead[
                "Pitch_Category"
            ] = result[
                "pitch"
            ]

            lead[
                "Business_Type"
            ] = result[
                "biz"
            ]

            lead[
                "Product_Category"
            ] = result[
                "prod"
            ]

            successful_rows.append(
                lead
            )

        # ----------------------------------------------------
        # Save only successful AI results.
        # ----------------------------------------------------

        if successful_rows:

            append_partial_rows(
                successful_rows,
                fieldnames,
            )

            stats[
                "ai_done"
            ] += len(
                successful_rows
            )

            stats[
                "processed"
            ] += len(
                successful_rows
            )

        # ----------------------------------------------------
        # Unresolved leads remain pending.
        # ----------------------------------------------------

        if unresolved_count:

            stats[
                "pending"
            ] += unresolved_count

            print(
                f"   ⚠️ "
                f"{unresolved_count}/"
                f"{len(batch)} leads remain "
                f"UNRESOLVED and will be retried "
                f"on the next run."
            )

        # ----------------------------------------------------
        # Adaptive batch sleep
        # ----------------------------------------------------

        if (
            consecutive_rate_limits
            >= 2
        ):

            current_batch_sleep = min(
                current_batch_sleep
                * 1.5,
                MAX_BATCH_SLEEP,
            )

        elif (
            consecutive_rate_limits
            == 0
            and current_batch_sleep
            > BASE_BATCH_SLEEP
        ):

            current_batch_sleep = max(
                current_batch_sleep
                * 0.9,
                BASE_BATCH_SLEEP,
            )

        time.sleep(
            current_batch_sleep
        )

    # ========================================================
    # FINAL COMPLETION CHECK
    # ========================================================

    final_processed = load_processed_domains(
        PARTIAL_OUTPUT_FILE
    )

    remaining_domains = []

    for lead in all_leads:

        domain = normalize_domain(
            lead.get(
                "Domain",
                ""
            )
        )

        if domain not in final_processed:

            remaining_domains.append(
                domain
            )

    remaining_count = len(
        remaining_domains
    )

    print()
    print("=" * 72)
    print(
        "📊 CATEGORIZER RUN SUMMARY"
    )
    print("=" * 72)

    print(
        f"⚡ Auto processed    : "
        f"{stats['auto_done']:,}"
    )

    print(
        f"🤖 AI processed      : "
        f"{stats['ai_done']:,}"
    )

    print(
        f"🧩 Single fallbacks  : "
        f"{stats['single_fallback_saved']:,}"
    )

    print(
        f"🔁 Retries           : "
        f"{stats['retries']:,}"
    )

    print(
        f"⏳ Rate limits       : "
        f"{stats['rate_limit_hits']:,}"
    )

    print(
        f"⚠️ Pending unresolved: "
        f"{remaining_count:,}"
    )

    if quota_exhausted:

        print(
            "🛑 Quota wall        : HIT — stopped early, "
            "resume on next run once quota resets."
        )

    print(
        f"📁 Partial output    : "
        f"{PARTIAL_OUTPUT_FILE}"
    )

    print()

    # ========================================================
    # COMPLETE
    # ========================================================

    if remaining_count == 0:

        if finalize_output():

            print(
                "🎉 ALL LEADS CATEGORIZED SUCCESSFULLY!"
            )

            print(
                f"📁 Final output: "
                f"{OUTPUT_FILE}"
            )

            print("=" * 72)

            return 0

        print(
            "❌ All leads processed, "
            "but finalization failed."
        )

        return 1

    # ========================================================
    # INCOMPLETE
    # ========================================================

    print(
        "⚠️ Categorizer incomplete."
    )

    print(
        "   Partial results safely saved."
    )

    print(
        "   Next run will resume only unresolved leads."
    )

    print(
        "   Final CSV will NOT be created until "
        "everything is complete."
    )

    print("=" * 72)

    # IMPORTANT:
    # Return non-zero so master controller knows this stage
    # did not finish successfully.
    return 1


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    raise SystemExit(
        main()
    )
