```python
import csv
import os
import re
import time
import threading
import concurrent.futures
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# BAWA SMART X-RAY SCANNER v3.1 FINAL
# ------------------------------------------------------------
# INPUT:
#   premium_domains.txt
#
# OUTPUT:
#   Ultimate_God_Leads.csv
#
# CACHE:
#   scanned_cache.txt
#
# FEATURES:
#   ✅ HTTPS-first scanning
#   ✅ HTTP fallback
#   ✅ Redirect handling
#   ✅ Thread-local sessions
#   ✅ Connection pooling
#   ✅ Request retries
#   ✅ Response-size protection
#   ✅ Retry-safe cache
#   ✅ Terminal DEAD handling for network failures
#   ✅ Unexpected errors remain visible/retryable
#   ✅ Email extraction
#   ✅ Phone extraction
#   ✅ Social discovery
#   ✅ Tech / Ads detection
#   ✅ Business classification
#   ✅ Product/Niche classification
#   ✅ Brand-stage detection
#   ✅ Intent detection
#   ✅ Thread-safe CSV writing
#   ✅ Existing pipeline-compatible output
# ============================================================


# ============================================================
# CONFIGURATION
# ============================================================

INPUT_FILE = "premium_domains.txt"
OUTPUT_FILE = "Ultimate_God_Leads.csv"
CACHE_FILE = "scanned_cache.txt"


# ------------------------------------------------------------
# Concurrency
# ------------------------------------------------------------

MAX_WORKERS = int(
    os.environ.get(
        "XRAY_WORKERS",
        "32"
    )
)


# ------------------------------------------------------------
# HTTP
# ------------------------------------------------------------

CONNECT_TIMEOUT = 5
READ_TIMEOUT = 12

REQUEST_TIMEOUT = (
    CONNECT_TIMEOUT,
    READ_TIMEOUT
)

MAX_RETRIES_PER_URL = 2

# Maximum HTML/body bytes to inspect.
# Protects RAM from giant websites.
MAX_RESPONSE_BYTES = 2_500_000


# ------------------------------------------------------------
# Content
# ------------------------------------------------------------

MAX_TITLE_LENGTH = 150
MAX_META_LENGTH = 500
MAX_PAGE_TEXT_LENGTH = 1000


# ------------------------------------------------------------
# Optional translation
# ------------------------------------------------------------

ENABLE_TRANSLATION = False
TRANSLATE_MAX_LENGTH = 500


# ------------------------------------------------------------
# CSV retention
# ------------------------------------------------------------

MIN_USEFUL_SIGNALS = 1


# ============================================================
# CSV SCHEMA
# ------------------------------------------------------------
# Existing downstream categorizer depends on these names.
# Do not rename casually.
# ============================================================

KEYS = [
    "Domain",
    "Brand_Stage",
    "Title",
    "Meta_Description",
    "Page_Text",
    "Emails",
    "Phones",
    "Socials_Found",
    "Tech_Stack_Ads",
    "Intent_Trust",
    "Business_Type",
    "Product_Category",
]


# ============================================================
# THREADING
# ============================================================

FILE_LOCK = threading.Lock()
THREAD_LOCAL = threading.local()


# ============================================================
# OPTIONAL TRANSLATOR
# ============================================================

TRANSLATOR_AVAILABLE = False

if ENABLE_TRANSLATION:

    try:
        from deep_translator import GoogleTranslator

        TRANSLATOR_AVAILABLE = True

    except ImportError:
        print(
            "⚠️ deep-translator not installed. "
            "Translation disabled."
        )


def translate_to_english(
    text,
    max_len=TRANSLATE_MAX_LENGTH
):
    """
    Optional language translation.

    Disabled by default because the next AI stage
    can already process multilingual website signals.
    """

    if not ENABLE_TRANSLATION:
        return text

    if not text:
        return text

    text = text.strip()

    if len(text) < 10:
        return text

    if not TRANSLATOR_AVAILABLE:
        return text

    try:

        ascii_ratio = (
            sum(
                1
                for c in text
                if ord(c) < 128
            )
            / max(
                1,
                len(text)
            )
        )

        # Already mostly English/Roman.
        if ascii_ratio > 0.85:
            return text

        translated = GoogleTranslator(
            source="auto",
            target="en"
        ).translate(
            text[:max_len]
        )

        return (
            translated
            if translated
            else text
        )

    except Exception:
        return text


# ============================================================
# HTTP HEADERS
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,image/avif,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Language": (
        "en-US,en;q=0.9"
    ),
    "Connection": "keep-alive",
}


# ============================================================
# HTTP SESSION
# ============================================================

def get_session():
    """
    One requests.Session per thread.

    This gives:
        - connection reuse
        - connection pooling
        - lower TCP overhead
        - thread-safe session separation
    """

    session = getattr(
        THREAD_LOCAL,
        "session",
        None
    )

    if session is not None:
        return session

    session = requests.Session()

    retry = Retry(
        total=MAX_RETRIES_PER_URL,
        connect=MAX_RETRIES_PER_URL,
        read=MAX_RETRIES_PER_URL,
        status=MAX_RETRIES_PER_URL,
        backoff_factor=0.7,
        status_forcelist=[
            408,
            429,
            500,
            502,
            503,
            504,
        ],
        allowed_methods=[
            "GET",
        ],
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=MAX_WORKERS,
        pool_maxsize=MAX_WORKERS,
    )

    session.mount(
        "https://",
        adapter
    )

    session.mount(
        "http://",
        adapter
    )

    session.headers.update(
        HEADERS
    )

    THREAD_LOCAL.session = session

    return session


# ============================================================
# TEXT UTILITIES
# ============================================================

def clean_text(
    value,
    max_len=None
):
    """
    Normalize whitespace and remove problematic characters.
    """

    if not value:
        return ""

    value = str(value)

    value = (
        value
        .replace("\x00", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("\t", " ")
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    value = value.strip()

    if max_len:
        value = value[:max_len]

    return value


def normalize_domain(
    domain
):
    """
    Normalize domain names consistently.

    Handles:
        protocol
        www
        paths
        query strings
        fragments
        trailing dots
    """

    if not domain:
        return ""

    domain = (
        domain
        .strip()
        .lower()
    )

    if not domain:
        return ""

    # Remove protocol
    domain = re.sub(
        r"^[a-z][a-z0-9+.-]*://",
        "",
        domain,
        flags=re.I,
    )

    # Remove www
    domain = re.sub(
        r"^www\.",
        "",
        domain,
        flags=re.I,
    )

    # Remove path/query/fragment
    domain = re.split(
        r"[/?#]",
        domain,
        maxsplit=1
    )[0]

    return domain.rstrip(".")


def root_of_domain(
    domain
):
    domain = normalize_domain(
        domain
    )

    if not domain:
        return ""

    return domain.split(".")[0]


# ============================================================
# BODY TEXT EXTRACTION
# ============================================================

NAV_WORDS = {
    "home",
    "about",
    "contact",
    "login",
    "sign",
    "signup",
    "register",
    "menu",
    "navigation",
    "search",
    "cart",
    "checkout",
    "privacy",
    "policy",
    "terms",
    "cookie",
    "copyright",
    "all rights reserved",
    "skip",
    "close",
    "open",
    "read more",
    "learn more",
    "next",
    "previous",
}


def extract_meaningful_body_text(
    soup,
    domain
):
    """
    Remove obvious webpage noise and extract useful content.
    """

    if not soup:
        return ""

    # Remove non-visible/noise elements.
    for tag in soup(
        [
            "script",
            "style",
            "noscript",
            "svg",
            "iframe",
            "canvas",
            "template",
        ]
    ):
        tag.decompose()

    root = root_of_domain(
        domain
    )

    chunks = []
    seen = set()

    candidates = soup.find_all(
        [
            "main",
            "article",
            "section",
            "header",
            "footer",
            "p",
            "h1",
            "h2",
            "h3",
            "li",
        ]
    )

    for element in candidates:

        text = clean_text(
            element.get_text(
                " ",
                strip=True
            )
        )

        if len(text) < 20:
            continue

        lower = text.lower()

        if lower in seen:
            continue

        # Skip navigation boilerplate.
        if (
            lower in NAV_WORDS
            or lower.startswith("cookie")
        ):
            continue

        # Obvious hostname spam.
        if (
            root
            and lower.count(root) >= 5
            and len(lower) < 250
        ):
            continue

        seen.add(lower)
        chunks.append(text)

        if len(
            " ".join(chunks)
        ) >= MAX_PAGE_TEXT_LENGTH:
            break

    return clean_text(
        " ".join(chunks),
        MAX_PAGE_TEXT_LENGTH
    )


# ============================================================
# META
# ============================================================

def get_meta_description(
    soup
):
    if not soup:
        return ""

    meta = soup.find(
        "meta",
        attrs={
            "name": re.compile(
                r"^description$",
                re.I
            )
        }
    )

    if meta:

        value = (
            meta.get("content")
            or ""
        )

        if value.strip():
            return clean_text(
                value,
                MAX_META_LENGTH
            )

    og = soup.find(
        "meta",
        attrs={
            "property": re.compile(
                r"^og:description$",
                re.I
            )
        }
    )

    if og:

        return clean_text(
            og.get("content")
            or "",
            MAX_META_LENGTH
        )

    return ""


# ============================================================
# EMAILS
# ============================================================

EMAIL_PATTERN = re.compile(
    r"\b[a-zA-Z0-9._%+\-]+"
    r"@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"
)

BAD_EMAIL_FRAGMENTS = {
    "example",
    "example.com",
    "test@",
    "sentry",
    "wix",
    "wordpress",
    "domain.com",
    "email@example",
    "yourmail",
}


def extract_emails(
    html,
    soup
):

    found = set()

    for email in re.findall(
        EMAIL_PATTERN,
        html or ""
    ):

        email = (
            email
            .strip()
            .lower()
            .rstrip(".")
        )

        if not email:
            continue

        if any(
            bad in email
            for bad in BAD_EMAIL_FRAGMENTS
        ):
            continue

        found.add(
            email
        )

    # mailto links
    if soup:

        for tag in soup.find_all(
            "a",
            href=True
        ):

            href = tag.get(
                "href",
                ""
            ).strip()

            if href.lower().startswith(
                "mailto:"
            ):

                email = (
                    href[7:]
                    .split("?")[0]
                    .strip()
                    .lower()
                )

                if EMAIL_PATTERN.fullmatch(
                    email
                ):

                    if not any(
                        bad in email
                        for bad in BAD_EMAIL_FRAGMENTS
                    ):

                        found.add(
                            email
                        )

    return sorted(
        found
    )


# ============================================================
# PHONES
# ============================================================

PHONE_PATTERN = re.compile(
    r"(?<!\d)"
    r"(?:\+?\d{1,3}[\s().-]?)?"
    r"(?:\(?\d{2,4}\)?[\s.-]?)?"
    r"\d{3,4}"
    r"[\s.-]?"
    r"\d{3,4}"
    r"(?!\d)"
)


def normalize_phone(
    phone
):

    digits = re.sub(
        r"\D",
        "",
        phone
    )

    if not (
        8
        <= len(digits)
        <= 15
    ):
        return None

    # Reject highly repetitive fake numbers.
    if len(set(digits)) <= 2:
        return None

    return phone.strip()


def extract_phones(
    html,
    soup
):

    found = set()

    # tel: links
    if soup:

        for tag in soup.find_all(
            "a",
            href=True
        ):

            href = tag.get(
                "href",
                ""
            ).strip()

            if href.lower().startswith(
                "tel:"
            ):

                phone = normalize_phone(
                    href[4:]
                )

                if phone:
                    found.add(phone)

    # Raw HTML/text fallback
    for phone in re.findall(
        PHONE_PATTERN,
        html or ""
    ):

        normalized = normalize_phone(
            phone
        )

        if normalized:
            found.add(
                normalized
            )

    return sorted(
        found
    )[:5]


# ============================================================
# SOCIALS
# ============================================================

SOCIAL_DOMAINS = {
    "instagram.com": "Instagram",
    "linkedin.com": "LinkedIn",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "twitter.com": "Twitter/X",
    "x.com": "Twitter/X",
    "facebook.com": "Facebook",
    "tiktok.com": "TikTok",
    "threads.net": "Threads",
    "pinterest.com": "Pinterest",
}


def extract_socials(
    soup
):

    found = set()

    if not soup:
        return []

    for tag in soup.find_all(
        "a",
        href=True
    ):

        href = (
            tag.get(
                "href",
                ""
            )
            .strip()
            .lower()
        )

        if not href:
            continue

        try:

            parsed = urlparse(
                href
            )

            hostname = (
                parsed.netloc
                .lower()
                .replace(
                    "www.",
                    ""
                )
            )

        except Exception:
            hostname = ""

        for platform_domain, name in SOCIAL_DOMAINS.items():

            if (
                hostname == platform_domain
                or hostname.endswith(
                    "." + platform_domain
                )
            ):

                found.add(
                    name
                )

    return sorted(
        found
    )


# ============================================================
# TECH STACK / ADS
# ============================================================

TECH_SIGNATURES = {

    "Meta Pixel": [
        "fbq(",
        "connect.facebook.net",
        "facebook pixel",
    ],

    "Google Analytics": [
        "gtag(",
        "google-analytics.com",
        "googletagmanager.com",
    ],

    "Google Ads": [
        "googleadservices.com",
        "google_conversion",
        "conversion.js",
    ],

    "TikTok Pixel": [
        "analytics.tiktok.com",
        "ttq.load",
        "tiktok pixel",
    ],

    "Shopify": [
        "cdn.shopify.com",
        "shopify",
        "myshopify.com",
    ],

    "WooCommerce": [
        "woocommerce",
        "wc-ajax",
    ],

    "Webflow": [
        "webflow",
        "website-files.com",
    ],

    "Wix": [
        "wix.com",
        "wixstatic.com",
    ],

    "WordPress": [
        "wp-content",
        "wp-includes",
        "wordpress",
    ],

    "Klaviyo": [
        "klaviyo",
    ],

    "HubSpot": [
        "hubspot",
    ],

    "Stripe": [
        "js.stripe.com",
        "stripe.com",
    ],

    "Intercom": [
        "intercom",
        "widget.intercom.io",
    ],
}


def detect_tech_stack(
    html_lower
):

    found = []

    for technology, signatures in TECH_SIGNATURES.items():

        if any(
            signature.lower()
            in html_lower
            for signature in signatures
        ):

            found.append(
                technology
            )

    return found


# ============================================================
# BUSINESS CLASSIFICATION
# ============================================================

BUSINESS_RULES = {

    "SaaS / Software": [
        "free trial",
        "start free",
        "book a demo",
        "request a demo",
        "schedule a demo",
        "dashboard",
        "sign up free",
        "api",
        "integration",
        "software",
        "platform",
        "saas",
        "automate",
        "workflow",
        "crm",
        "erp",
        "subscription",
        "pricing plan",
    ],

    "Startup": [
        "join the waitlist",
        "join waitlist",
        "early access",
        "get early access",
        "we are building",
        "we're building",
        "backed by",
        "seed round",
        "series a",
        "pre-seed",
        "in beta",
        "beta access",
        "founding team",
        "we raised",
        "venture capital",
        "launching soon",
    ],

    "Physical Product Brand": [
        "add to cart",
        "add to bag",
        "buy now",
        "shop now",
        "free shipping",
        "order now",
        "in stock",
        "out of stock",
        "checkout",
        "collection",
        "all products",
        "new arrivals",
        "best sellers",
        "shop all",
        "cash on delivery",
        "track your order",
        "return policy",
    ],

    "Service Business": [
        "book a call",
        "book a free call",
        "get a quote",
        "free quote",
        "our services",
        "what we do",
        "our work",
        "case studies",
        "consultation",
        "agency",
        "we help",
        "we specialize",
        "our process",
        "portfolio",
        "client results",
        "work with us",
        "get in touch",
    ],
}


def classify_business(
    combined_text
):

    text = (
        combined_text
        or ""
    ).lower()

    scores = {}

    for business_type, keywords in BUSINESS_RULES.items():

        score = 0

        for keyword in keywords:

            if keyword in text:
                score += 1

        if score:
            scores[
                business_type
            ] = score

    if not scores:
        return "Unclear"

    return max(
        scores,
        key=scores.get
    )


# ============================================================
# PRODUCT CATEGORY
# ============================================================

PRODUCT_CATEGORIES = {

    "Fashion & Apparel": [
        "clothing",
        "fashion",
        "apparel",
        "wear",
        "outfit",
        "dress",
        "shirt",
        "t-shirt",
        "hoodie",
        "shoes",
        "sneakers",
        "footwear",
        "wardrobe",
        "streetwear",
        "ethnic wear",
        "kurta",
        "saree",
    ],

    "Beauty & Skincare": [
        "skincare",
        "beauty",
        "serum",
        "moisturizer",
        "cosmetic",
        "cosmetics",
        "glow",
        "hair care",
        "shampoo",
        "conditioner",
        "makeup",
        "nail",
        "fragrance",
        "perfume",
        "sunscreen",
        "face wash",
    ],

    "Food & Beverage": [
        "food",
        "snack",
        "beverage",
        "drink",
        "coffee",
        "tea",
        "nutrition",
        "protein",
        "chocolate",
        "biscuit",
        "sauce",
        "spice",
        "organic food",
        "health food",
        "meal",
        "recipe",
        "restaurant",
        "cafe",
        "bakery",
    ],

    "Pets": [
        "pet",
        "dog",
        "cat",
        "paw",
        "fur",
        "vet",
        "animal",
        "puppy",
        "kitten",
        "pet food",
        "pet care",
        "grooming",
    ],

    "Home & Decor": [
        "home decor",
        "furniture",
        "interior",
        "living room",
        "bedroom",
        "candle",
        "wall art",
        "cushion",
        "lamp",
        "rug",
        "curtain",
        "mattress",
        "sofa",
        "kitchen",
        "bathroom",
        "storage",
    ],

    "Health & Fitness": [
        "fitness",
        "gym",
        "supplement",
        "workout",
        "wellness",
        "yoga",
        "diet",
        "weight loss",
        "muscle",
        "protein powder",
        "pre workout",
        "health",
        "ayurved",
        "immunity",
        "vitamin",
        "omega",
        "therapy",
    ],

    "Kids & Baby": [
        "kids",
        "baby",
        "toddler",
        "children",
        "toy",
        "parenting",
        "infant",
        "newborn",
        "maternity",
        "diaper",
        "stroller",
        "school bag",
        "kids wear",
    ],

    "Tech & Gadgets": [
        "gadget",
        "device",
        "electronics",
        "wireless",
        "smart home",
        "charger",
        "earphone",
        "headphone",
        "laptop",
        "mobile",
        "phone case",
        "power bank",
        "camera",
        "drone",
        "wearable",
        "smartwatch",
    ],

    "Education & Coaching": [
        "course",
        "learn",
        "education",
        "training",
        "skill",
        "tutorial",
        "coaching",
        "mentor",
        "certification",
        "bootcamp",
        "masterclass",
        "workshop",
        "study",
        "exam prep",
        "upskill",
    ],

    "Finance & Fintech": [
        "invest",
        "finance",
        "crypto",
        "trading",
        "wealth",
        "insurance",
        "loan",
        "mutual fund",
        "stock",
        "portfolio",
        "fintech",
        "banking",
        "payment",
        "wallet",
        "tax",
        "accounting",
    ],

    "Real Estate": [
        "real estate",
        "property",
        "flat",
        "apartment",
        "villa",
        "plot",
        "buy home",
        "rent",
        "commercial space",
        "office space",
        "realty",
        "housing",
    ],

    "Travel & Hospitality": [
        "travel",
        "hotel",
        "resort",
        "holiday",
        "vacation",
        "tour",
        "trek",
        "adventure",
        "flight",
        "booking",
        "hospitality",
        "stay",
    ],

    "Gaming & Entertainment": [
        "gaming",
        "game",
        "esports",
        "streaming",
        "entertainment",
        "music",
        "podcast",
        "creator",
        "content",
        "media",
        "film",
        "video",
    ],

    "Sustainable & Eco": [
        "sustainable",
        "eco",
        "organic",
        "green",
        "zero waste",
        "recyclable",
        "environment",
        "natural",
        "vegan",
        "cruelty free",
        "biodegradable",
    ],
}


def classify_product(
    text
):

    text = (
        text
        or ""
    ).lower()

    category_scores = {}

    for category, keywords in PRODUCT_CATEGORIES.items():

        score = 0

        for keyword in keywords:

            if keyword in text:

                # Phrases carry slightly more weight.
                if " " in keyword:
                    score += 3
                else:
                    score += 1

        if score:
            category_scores[
                category
            ] = score

    if not category_scores:
        return "General / Other"

    return max(
        category_scores,
        key=category_scores.get
    )


# ============================================================
# BRAND STAGE
# ============================================================

PRELAUNCH_SIGNALS = [
    "coming soon",
    "launching soon",
    "under construction",
    "check back soon",
    "check back for an update",
    "opening soon",
    "be the first to know",
    "join the waitlist",
    "early access",
]


PARKED_SIGNALS = [
    "domain for sale",
    "this domain is for sale",
    "buy this domain",
    "parked domain",
    "hugedomains",
    "sedoparking",
    "undeveloped",
    "domain parking",
    "welcome to nginx",
]


def detect_brand_stage(
    combined_text,
    html_lower
):

    text = (
        combined_text
        + " "
        + html_lower[:500000]
    ).lower()

    stage_signals = []

    if any(
        signal in text
        for signal in PRELAUNCH_SIGNALS
    ):

        stage_signals.append(
            "Pre-Launch"
        )

    if (
        "shopify" in text
        and (
            "password page" in text
            or "opening soon" in text
            or "enter store using password"
            in text
        )
    ):

        stage_signals.append(
            "Shopify Password Page"
        )

    if (
        "linktr.ee" in text
        or "linktree" in text
        or "bento.me" in text
        or "beacons.ai" in text
    ):

        stage_signals.append(
            "Link-in-Bio"
        )

    if stage_signals:

        # Preserve order and remove duplicates.
        return " | ".join(
            dict.fromkeys(
                stage_signals
            )
        )

    return "Live"


def is_parked_page(
    title,
    meta,
    body
):

    combined = (
        title
        + " "
        + meta
        + " "
        + body
    ).lower()

    return any(
        signal in combined
        for signal in PARKED_SIGNALS
    )


# ============================================================
# INTENT
# ============================================================

INTENT_RULES = {

    "E-Commerce": [
        "add to cart",
        "add to bag",
        "checkout",
        "buy now",
        "shop now",
        "free shipping",
    ],

    "SaaS/B2B": [
        "book a demo",
        "request a demo",
        "free trial",
        "pricing",
        "platform",
        "api",
        "integration",
    ],

    "Booking/Appointment": [
        "book now",
        "book a call",
        "appointment",
        "schedule an appointment",
        "reservation",
    ],

    "Lead Generation": [
        "get a quote",
        "contact us",
        "request a quote",
        "talk to us",
        "get in touch",
    ],

    "Subscription": [
        "subscribe",
        "monthly plan",
        "annual plan",
        "membership",
        "subscription",
    ],
}


def detect_intent(
    text,
    emails,
    phones,
    socials,
    tech_stack
):

    combined = (
        text
        or ""
    ).lower()

    found = []

    for intent, keywords in INTENT_RULES.items():

        if any(
            keyword in combined
            for keyword in keywords
        ):

            found.append(
                intent
            )

    if emails:
        found.append(
            "Email Contact"
        )

    if phones:
        found.append(
            "Phone Contact"
        )

    if socials:
        found.append(
            "Social Presence"
        )

    if tech_stack:
        found.append(
            "Tech/Tracking"
        )

    return list(
        dict.fromkeys(
            found
        )
    )


# ============================================================
# SAFE RESPONSE READER
# ============================================================

def read_response_limited(
    response
):
    """
    Read response incrementally and stop at a hard byte limit.
    """

    content = bytearray()

    try:

        for chunk in response.iter_content(
            chunk_size=65536
        ):

            if not chunk:
                continue

            remaining = (
                MAX_RESPONSE_BYTES
                - len(content)
            )

            if remaining <= 0:
                break

            content.extend(
                chunk[:remaining]
            )

            if len(content) >= MAX_RESPONSE_BYTES:
                break

    except Exception:
        return b""

    return bytes(
        content
    )


# ============================================================
# WEBSITE FETCH
# ============================================================

def fetch_website(
    domain
):

    """
    Try:
        HTTPS
        HTTP

    Redirects are explicitly enabled.

    Returns:
        {
            "url": final_url,
            "status_code": status,
            "html": html
        }

    or:
        None
    """

    session = get_session()

    urls = [
        f"https://{domain}",
        f"http://{domain}",
    ]

    for url in urls:

        response = None

        try:

            response = session.get(
                url,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
                stream=True,
                verify=True,
            )

            status_code = response.status_code

            if status_code >= 400:
                continue

            content_type = (
                response.headers
                .get(
                    "Content-Type",
                    ""
                )
                .lower()
            )

            # Ignore obvious non-HTML assets.
            if (
                content_type
                and not any(
                    allowed in content_type
                    for allowed in (
                        "text/html",
                        "application/xhtml+xml",
                        "text/plain",
                    )
                )
            ):
                continue

            raw = read_response_limited(
                response
            )

            if not raw:
                continue

            encoding = (
                response.encoding
                or "utf-8"
            )

            try:

                html = raw.decode(
                    encoding,
                    errors="ignore"
                )

            except Exception:

                html = raw.decode(
                    "utf-8",
                    errors="ignore"
                )

            if not html.strip():
                continue

            return {
                "url": response.url or url,
                "status_code": status_code,
                "html": html,
            }

        except (
            requests.exceptions.SSLError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.TooManyRedirects,
            requests.exceptions.RequestException,
        ):
            continue

        except Exception:
            continue

        finally:

            if response is not None:

                try:
                    response.close()
                except Exception:
                    pass

    return None


# ============================================================
# ADVANCED DATA EXTRACTION
# ============================================================

def extract_advanced_data(
    domain
):

    domain = normalize_domain(
        domain
    )

    if not domain:
        return None

    fetched = fetch_website(
        domain
    )

    # --------------------------------------------------------
    # IMPORTANT:
    # None means network/request resolution failure.
    # Caller will handle this as terminal DEAD after request
    # layer retries so master controller doesn't loop forever.
    # --------------------------------------------------------

    if not fetched:
        return None

    html = fetched[
        "html"
    ]

    html_lower = html.lower()

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    lead_data = {
        "Domain": domain,
        "Brand_Stage": "Live",
        "Title": "",
        "Meta_Description": "",
        "Page_Text": "",
        "Emails": "",
        "Phones": "",
        "Socials_Found": "",
        "Tech_Stack_Ads": "",
        "Intent_Trust": "",
        "Business_Type": "",
        "Product_Category": "",
        "Status": "LIVE",
    }

    # ========================================================
    # TITLE
    # ========================================================

    title_tag = soup.find(
        "title"
    )

    if title_tag:

        lead_data[
            "Title"
        ] = clean_text(
            title_tag.get_text(
                " ",
                strip=True
            ),
            MAX_TITLE_LENGTH
        )

    # ========================================================
    # META
    # ========================================================

    meta_description = (
        get_meta_description(
            soup
        )
    )

    if meta_description:

        lead_data[
            "Meta_Description"
        ] = translate_to_english(
            meta_description,
            TRANSLATE_MAX_LENGTH
        )

    # ========================================================
    # BODY
    # ========================================================

    body_text = (
        extract_meaningful_body_text(
            soup,
            domain
        )
    )

    body_text = translate_to_english(
        body_text,
        TRANSLATE_MAX_LENGTH
    )

    lead_data[
        "Page_Text"
    ] = clean_text(
        body_text,
        MAX_PAGE_TEXT_LENGTH
    )

    # ========================================================
    # EMAIL
    # ========================================================

    emails = extract_emails(
        html,
        soup
    )

    if emails:

        lead_data[
            "Emails"
        ] = " | ".join(
            emails[:5]
        )

    # ========================================================
    # PHONE
    # ========================================================

    phones = extract_phones(
        html,
        soup
    )

    if phones:

        lead_data[
            "Phones"
        ] = " | ".join(
            phones[:5]
        )

    # ========================================================
    # SOCIALS
    # ========================================================

    socials = extract_socials(
        soup
    )

    if socials:

        lead_data[
            "Socials_Found"
        ] = " + ".join(
            socials
        )

    # ========================================================
    # TECH STACK
    # ========================================================

    tech_stack = detect_tech_stack(
        html_lower
    )

    if tech_stack:

        lead_data[
            "Tech_Stack_Ads"
        ] = " + ".join(
            tech_stack
        )

    # ========================================================
    # CLASSIFICATION TEXT
    # ========================================================

    classify_text = clean_text(
        " ".join(
            [
                lead_data["Title"],
                lead_data[
                    "Meta_Description"
                ],
                lead_data[
                    "Page_Text"
                ],
            ]
        )
    ).lower()

    # ========================================================
    # BRAND STAGE
    # ========================================================

    lead_data[
        "Brand_Stage"
    ] = detect_brand_stage(
        classify_text,
        html_lower
    )

    # ========================================================
    # PARKED PAGE
    # ========================================================

    if is_parked_page(
        lead_data["Title"],
        lead_data[
            "Meta_Description"
        ],
        lead_data[
            "Page_Text"
        ],
    ):

        lead_data[
            "Brand_Stage"
        ] = "Parked / Domain For Sale"

    # ========================================================
    # BUSINESS TYPE
    # ========================================================

    lead_data[
        "Business_Type"
    ] = classify_business(
        classify_text
    )

    # ========================================================
    # PRODUCT CATEGORY
    # ========================================================

    lead_data[
        "Product_Category"
    ] = classify_product(
        classify_text
    )

    # ========================================================
    # INTENT
    # ========================================================

    intents = detect_intent(
        classify_text,
        emails,
        phones,
        socials,
        tech_stack
    )

    if intents:

        lead_data[
            "Intent_Trust"
        ] = " | ".join(
            intents
        )

    return lead_data


# ============================================================
# SIGNAL QUALITY
# ============================================================

def count_useful_signals(
    lead
):

    count = 0

    if lead.get(
        "Emails"
    ):
        count += 1

    if lead.get(
        "Phones"
    ):
        count += 1

    if lead.get(
        "Socials_Found"
    ):
        count += 1

    if lead.get(
        "Tech_Stack_Ads"
    ):
        count += 1

    if lead.get(
        "Intent_Trust"
    ):
        count += 1

    if lead.get(
        "Brand_Stage"
    ) not in (
        "",
        "Live",
    ):
        count += 1

    if lead.get(
        "Page_Text"
    ):
        count += 1

    if lead.get(
        "Title"
    ):
        count += 1

    return count


# ============================================================
# CSV SAVE
# ============================================================

def save_lead(
    lead
):

    if not lead:
        return False

    if lead.get(
        "Status"
    ) != "LIVE":
        return False

    if (
        count_useful_signals(
            lead
        )
        < MIN_USEFUL_SIGNALS
    ):
        return False

    with FILE_LOCK:

        file_exists = os.path.exists(
            OUTPUT_FILE
        )

        needs_header = (
            not file_exists
            or os.path.getsize(
                OUTPUT_FILE
            ) == 0
        )

        with open(
            OUTPUT_FILE,
            "a",
            newline="",
            encoding="utf-8"
        ) as csvfile:

            writer = csv.DictWriter(
                csvfile,
                fieldnames=KEYS,
                extrasaction="ignore"
            )

            if needs_header:
                writer.writeheader()

            writer.writerow(
                {
                    key: lead.get(
                        key,
                        ""
                    )
                    for key in KEYS
                }
            )

    return True


# ============================================================
# CACHE
# ============================================================

def load_scanned_cache():

    scanned = set()

    if not os.path.exists(
        CACHE_FILE
    ):
        return scanned

    try:

        with open(
            CACHE_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            for line in file:

                line = line.strip()

                if not line:
                    continue

                # New format:
                #
                # SUCCESS|example.com
                # DEAD|example.com
                #
                # Old format:
                #
                # example.com
                #
                # remains backward-compatible.

                if "|" in line:

                    status, domain = (
                        line.split(
                            "|",
                            1
                        )
                    )

                    status = status.strip()
                    domain = normalize_domain(
                        domain
                    )

                    if (
                        status
                        in {
                            "SUCCESS",
                            "DEAD",
                        }
                        and domain
                    ):

                        scanned.add(
                            domain
                        )

                else:

                    domain = normalize_domain(
                        line
                    )

                    if domain:
                        scanned.add(
                            domain
                        )

    except OSError as exc:

        print(
            f"⚠️ Cache read failed: {exc}"
        )

    return scanned


def append_cache(
    domain,
    status
):

    domain = normalize_domain(
        domain
    )

    if not domain:
        return

    if status not in {
        "SUCCESS",
        "DEAD",
    }:
        return

    with FILE_LOCK:

        with open(
            CACHE_FILE,
            "a",
            encoding="utf-8"
        ) as cache:

            cache.write(
                f"{status}|{domain}\n"
            )


# ============================================================
# PROCESS ONE DOMAIN
# ============================================================

def process_domain(
    domain
):

    domain = normalize_domain(
        domain
    )

    if not domain:

        return {
            "domain": domain,
            "status": "DEAD",
            "lead": None,
        }

    try:

        lead = extract_advanced_data(
            domain
        )

        # ----------------------------------------------------
        # IMPORTANT PIPELINE RULE
        #
        # Network/request layer has already exhausted:
        #   MAX_RETRIES_PER_URL
        #
        # Therefore unresolved request-level failures become
        # DEAD for THIS scan cycle and are cached.
        #
        # This prevents master_controller from repeatedly
        # launching the same impossible domains forever.
        # ----------------------------------------------------

        if not lead:

            append_cache(
                domain,
                "DEAD"
            )

            return {
                "domain": domain,
                "status": "DEAD",
                "lead": None,
                "error": (
                    "Website unreachable, "
                    "request failed, or no usable "
                    "HTML response."
                ),
            }

        # ----------------------------------------------------
        # LIVE WEBSITE
        # ----------------------------------------------------

        if lead.get(
            "Status"
        ) == "LIVE":

            saved = save_lead(
                lead
            )

            if saved:

                append_cache(
                    domain,
                    "SUCCESS"
                )

                return {
                    "domain": domain,
                    "status": "SUCCESS",
                    "lead": lead,
                }

            # Website alive but signal quality below threshold.
            append_cache(
                domain,
                "DEAD"
            )

            return {
                "domain": domain,
                "status": "DEAD",
                "lead": lead,
            }

        # Any explicitly non-live state.
        append_cache(
            domain,
            "DEAD"
        )

        return {
            "domain": domain,
            "status": "DEAD",
            "lead": lead,
        }

    except (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.TooManyRedirects,
        requests.exceptions.SSLError,
        requests.exceptions.RequestException,
    ) as exc:

        # HTTP layer already retried.
        # Terminal DEAD prevents infinite master-controller loop.
        append_cache(
            domain,
            "DEAD"
        )

        return {
            "domain": domain,
            "status": "DEAD",
            "lead": None,
            "error": str(exc),
        }

    except Exception as exc:

        # ----------------------------------------------------
        # IMPORTANT:
        # Unexpected programming/data errors are NOT silently
        # converted into DEAD. They remain RETRY so the problem
        # stays visible to us.
        #
        # But because filter_2.done depends on retry_count,
        # master_controller can revisit this stage and expose
        # the failure rather than silently losing the domain.
        # ----------------------------------------------------

        return {
            "domain": domain,
            "status": "RETRY",
            "lead": None,
            "error": str(exc),
        }


# ============================================================
# INPUT LOADER
# ============================================================

def load_input_domains():

    if not os.path.exists(
        INPUT_FILE
    ):

        raise FileNotFoundError(
            INPUT_FILE
        )

    domains = set()

    with open(
        INPUT_FILE,
        "r",
        encoding="utf-8",
        errors="ignore"
    ) as file:

        for line in file:

            domain = normalize_domain(
                line
            )

            if domain:
                domains.add(
                    domain
                )

    return domains


# ============================================================
# MAIN
# ============================================================

def main():

    print()
    print("=" * 72)
    print("🔍 BAWA SMART X-RAY SCANNER v3.1 FINAL")
    print("=" * 72)
    print()

    start_time = time.time()

    # --------------------------------------------------------
    # Load input
    # --------------------------------------------------------

    try:

        all_domains = load_input_domains()

    except FileNotFoundError:

        print(
            f"❌ '{INPUT_FILE}' nahi mila."
        )

        return 1

    except Exception as exc:

        print(
            f"❌ Input read error: {exc}"
        )

        return 1

    # --------------------------------------------------------
    # Load completed cache
    # --------------------------------------------------------

    scanned_domains = (
        load_scanned_cache()
    )

    domains_to_scan = sorted(
        all_domains
        - scanned_domains
    )

    total = len(
        domains_to_scan
    )

    print(
        f"📊 Total unique domains : "
        f"{len(all_domains):,}"
    )

    print(
        f"⏭️ Already completed     : "
        f"{len(scanned_domains):,}"
    )

    print(
        f"🎯 Pending X-Ray         : "
        f"{total:,}"
    )

    print(
        f"🧵 Workers               : "
        f"{MAX_WORKERS}"
    )

    print()

    # --------------------------------------------------------
    # Nothing left
    # --------------------------------------------------------

    if total == 0:

        print(
            "✅ Saare domains already processed."
        )

        open(
            "filter_2.done",
            "w"
        ).close()

        return 0

    # --------------------------------------------------------
    # Ensure CSV exists with proper header
    # --------------------------------------------------------

    if (
        not os.path.exists(
            OUTPUT_FILE
        )
        or os.path.getsize(
            OUTPUT_FILE
        ) == 0
    ):

        with FILE_LOCK:

            with open(
                OUTPUT_FILE,
                "w",
                newline="",
                encoding="utf-8"
            ) as csvfile:

                writer = csv.DictWriter(
                    csvfile,
                    fieldnames=KEYS
                )

                writer.writeheader()

    # --------------------------------------------------------
    # Counters
    # --------------------------------------------------------

    success_count = 0
    dead_count = 0
    retry_count = 0
    processed_count = 0

    # --------------------------------------------------------
    # Thread pool
    # --------------------------------------------------------

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                process_domain,
                domain
            ): domain
            for domain in domains_to_scan
        }

        for future in concurrent.futures.as_completed(
            futures
        ):

            processed_count += 1

            domain = futures[
                future
            ]

            try:

                result = future.result()

            except Exception as exc:

                # Extra safety layer around worker itself.
                result = {
                    "domain": domain,
                    "status": "RETRY",
                    "lead": None,
                    "error": str(exc),
                }

            status = result.get(
                "status"
            )

            if status == "SUCCESS":

                success_count += 1

            elif status == "DEAD":

                dead_count += 1

            else:

                retry_count += 1

                error = result.get(
                    "error",
                    "unknown"
                )

                print(
                    f"\n⚠️ RETRY: "
                    f"{domain} -> "
                    f"{str(error)[:160]}"
                )

            # ------------------------------------------------
            # Live progress
            # ------------------------------------------------

            if (
                processed_count % 10 == 0
                or processed_count == total
            ):

                elapsed = (
                    time.time()
                    - start_time
                )

                rate = (
                    processed_count
                    / elapsed
                    if elapsed > 0
                    else 0
                )

                remaining = (
                    total
                    - processed_count
                )

                eta = (
                    remaining / rate
                    if rate > 0
                    else 0
                )

                print(
                    f"\r🚀 "
                    f"{processed_count:,}/{total:,} "
                    f"| LIVE {success_count:,} "
                    f"| DEAD {dead_count:,} "
                    f"| RETRY {retry_count:,} "
                    f"| {rate:.1f}/s "
                    f"| ETA {eta/60:.1f}m",
                    end="",
                    flush=True
                )

    print()
    print()

    # ========================================================
    # COMPLETION CONTRACT
    # ========================================================
    #
    # retry_count == 0
    #       ↓
    # filter_2.done
    #
    # retry_count > 0
    #       ↓
    # no filter_2.done
    #
    # This keeps unexpected errors visible.
    # ========================================================

    if retry_count == 0:

        open(
            "filter_2.done",
            "w"
        ).close()

        completed = True

    else:

        completed = False

    # ========================================================
    # SUMMARY
    # ========================================================

    total_time = (
        time.time()
        - start_time
    )

    print("=" * 72)
    print("🎉 X-RAY RUN COMPLETE")
    print("=" * 72)

    print(
        f"📊 Input domains      : "
        f"{len(all_domains):,}"
    )

    print(
        f"🔎 Processed          : "
        f"{processed_count:,}"
    )

    print(
        f"✅ Useful LIVE leads  : "
        f"{success_count:,}"
    )

    print(
        f"🗑️ DEAD / filtered    : "
        f"{dead_count:,}"
    )

    print(
        f"🔁 Unexpected RETRY   : "
        f"{retry_count:,}"
    )

    print(
        f"📁 Output             : "
        f"{OUTPUT_FILE}"
    )

    print(
        f"🗂️ Cache              : "
        f"{CACHE_FILE}"
    )

    print(
        f"🏁 Step completed     : "
        f"{completed}"
    )

    print(
        f"⏱️ Time               : "
        f"{total_time:.2f}s"
    )

    print("=" * 72)

    return 0


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    raise SystemExit(
        main()
    )
```
