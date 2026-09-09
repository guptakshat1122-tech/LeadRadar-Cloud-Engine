import os
import re
import time
from collections import defaultdict


# ============================================================
# BAWA DOMAIN FILTER v3.0
# ------------------------------------------------------------
# INPUT:
#   domain-names.txt
#
# OUTPUT:
#   premium_domains.txt
#
# IMPORTANT:
#   premium_domains.txt mein ONLY domain names likhe jaate hain.
#   Score/reason internal ranking ke liye use hote hain.
#
# FEATURES:
#   ✅ Strict domain normalization
#   ✅ Duplicate removal
#   ✅ Multi-TLD support
#   ✅ Longest-TLD matching (.co.in before .in)
#   ✅ Smart keyword scoring
#   ✅ Short keyword false-positive protection
#   ✅ Length scoring
#   ✅ Number/hyphen penalties
#   ✅ Brandability heuristics
#   ✅ Commercial intent scoring
#   ✅ Suspicious pattern filtering
#   ✅ Score-based ranking
#   ✅ Atomic output writing
#   ✅ Streaming input processing
#   ✅ Compatible with existing X-Ray scanner
# ============================================================


# ============================================================
# PATHS
# ============================================================

INPUT_FILE = "domain-names.txt"
OUTPUT_FILE = "premium_domains.txt"

# Optional debugging/report file.
# This does NOT get used by the next pipeline stage.
REPORT_FILE = "premium_domain_report.txt"


# ============================================================
# FILTER SETTINGS
# ============================================================

MIN_ROOT_LENGTH = 3
MAX_ROOT_LENGTH = 18

MIN_PREMIUM_SCORE = 50

# Keep output manageable.
# None = no maximum.
MAX_OUTPUT_DOMAINS = None


# ============================================================
# TARGET TLDS
# ------------------------------------------------------------
# Score is a heuristic, not an actual resale valuation.
# ============================================================

TLD_SCORES = {
    ".com": 40,
    ".ai": 35,
    ".in": 30,
    ".co": 28,
    ".io": 25,
    ".app": 24,
    ".tech": 20,
    ".gg": 18,
    ".so": 17,
    ".co.in": 17,
    ".health": 16,
    ".store": 15,
    ".shop": 15,
    ".net": 12,
    ".org": 10,
    ".co.uk": 10,
    ".us": 8,
}


# ============================================================
# KEYWORD DATABASE
# ------------------------------------------------------------
# Larger/commercially stronger terms receive more points.
# ============================================================

KEYWORDS = {

    "Beauty": {
        "beauty": 20,
        "skincare": 22,
        "cosmetics": 20,
        "cosmetic": 18,
        "derma": 16,
        "glow": 14,
        "glam": 12,
        "lash": 12,
        "hair": 12,
        "salon": 15,
        "aura": 10,
        "luxe": 12,
        "pure": 9,
        "face": 10,
        "makeup": 18,
        "nail": 12,
    },

    "Kids": {
        "kids": 18,
        "baby": 20,
        "child": 16,
        "children": 16,
        "tots": 14,
        "little": 12,
        "junior": 12,
        "mama": 14,
        "mom": 10,
        "toy": 14,
        "toys": 14,
        "play": 12,
        "tiny": 12,
        "cradle": 12,
    },

    "Fashion": {
        "fashion": 20,
        "wear": 14,
        "style": 14,
        "vogue": 15,
        "thread": 12,
        "threads": 12,
        "apparel": 16,
        "outfit": 12,
        "stitch": 12,
        "trend": 12,
        "closet": 12,
        "kicks": 14,
        "streetwear": 18,
    },

    "Tech": {
        "tech": 16,
        "saas": 20,
        "labs": 14,
        "api": 12,
        "dev": 12,
        "cloud": 18,
        "cyber": 18,
        "data": 14,
        "software": 18,
        "code": 14,
        "byte": 12,
        "stack": 16,
        "bot": 14,
        "sync": 14,
        "flow": 14,
        "platform": 18,
        "software": 18,
    },

    "AI": {
        "ai": 22,
        "agent": 20,
        "agents": 20,
        "ml": 16,
        "model": 16,
        "models": 16,
        "neural": 18,
        "prompt": 16,
        "genai": 22,
        "machinelearning": 22,
    },

    "Pets": {
        "pet": 16,
        "pets": 16,
        "paw": 16,
        "paws": 16,
        "tail": 12,
        "vet": 14,
        "bark": 12,
        "fur": 12,
        "meow": 12,
        "hound": 12,
        "puppy": 16,
        "kitten": 16,
    },

    "Home": {
        "home": 18,
        "decor": 18,
        "living": 14,
        "space": 12,
        "nest": 14,
        "craft": 12,
        "furn": 14,
        "furniture": 18,
        "wood": 10,
        "casa": 12,
        "room": 12,
        "vibe": 12,
    },

    "Ecommerce": {
        "shop": 16,
        "store": 16,
        "cart": 14,
        "buy": 14,
        "mart": 14,
        "brand": 14,
        "goods": 12,
        "deal": 12,
        "retail": 18,
        "market": 16,
    },

    "Health": {
        "health": 18,
        "fitness": 20,
        "fit": 10,
        "med": 12,
        "medical": 18,
        "clinic": 20,
        "cure": 14,
        "gym": 14,
        "wellness": 20,
        "dental": 18,
        "nutra": 16,
        "diet": 12,
        "vital": 12,
        "protein": 16,
        "therapy": 16,
        "doctor": 18,
    },

    "Finance": {
        "finance": 20,
        "fin": 10,
        "wealth": 20,
        "invest": 20,
        "capital": 20,
        "fund": 16,
        "trade": 16,
        "trading": 18,
        "crypto": 22,
        "coin": 14,
        "mint": 14,
        "tax": 12,
        "bank": 20,
        "pay": 14,
        "wallet": 18,
        "fintech": 20,
    },

    "RealEstate": {
        "realty": 20,
        "estate": 20,
        "property": 20,
        "prop": 10,
        "homes": 18,
        "home": 14,
        "build": 14,
        "infra": 14,
        "arch": 12,
        "land": 16,
        "agency": 16,
    },

    "Business": {
        "studio": 14,
        "media": 12,
        "consult": 16,
        "consulting": 18,
        "partners": 14,
        "group": 10,
        "growth": 16,
        "creative": 14,
        "event": 12,
        "events": 14,
        "agency": 16,
    },

    "Food": {
        "food": 18,
        "eats": 16,
        "brew": 14,
        "cafe": 16,
        "farm": 14,
        "fresh": 16,
        "bite": 14,
        "agro": 16,
        "dine": 14,
        "snack": 14,
        "sip": 12,
        "bakery": 16,
        "coffee": 16,
    },

    "Education": {
        "learn": 18,
        "edu": 14,
        "academy": 20,
        "skill": 16,
        "prep": 14,
        "brain": 14,
        "tutor": 20,
        "class": 16,
        "course": 18,
        "coach": 16,
        "education": 20,
    },
}


# ============================================================
# SHORT KEYWORDS
# ------------------------------------------------------------
# These are dangerous as substring matches.
#
# Example:
#   "ai" inside "mail" should NOT count.
# ============================================================

SHORT_KEYWORDS = {
    "ai",
    "ml",
    "api",
    "dev",
    "bot",
    "fit",
    "pet",
    "paw",
    "pay",
    "fin",
    "med",
    "lab",
    "app",
    "gym",
    "tax",
    "vet",
}


# ============================================================
# BASIC PATTERNS
# ============================================================

VALID_DOMAIN_PATTERN = re.compile(
    r"^[a-z0-9]+(?:-[a-z0-9]+)?$"
)

REPEATED_CHAR_PATTERN = re.compile(
    r"(.)\1\1+"
)

HEAVY_CONSONANT_PATTERN = re.compile(
    r"[bcdfghjklmnpqrstvwxyz]{5,}"
)


# ============================================================
# LOW QUALITY PREFIXES
# ------------------------------------------------------------
# Soft penalty only.
# ============================================================

WEAK_PREFIXES = {
    "get",
    "my",
    "the",
    "try",
    "use",
    "buy",
    "best",
}


# ============================================================
# NORMALIZATION
# ============================================================

def normalize_domain(raw_domain):
    """
    Convert raw domain text into a normalized hostname.

    Handles:
        HTTPS
        HTTP
        WWW
        trailing slash
        accidental whitespace
        trailing dot
    """

    if not raw_domain:
        return ""

    domain = raw_domain.strip().lower()

    if not domain:
        return ""

    # Remove protocol
    domain = re.sub(
        r"^[a-z][a-z0-9+.-]*://",
        "",
        domain,
        flags=re.IGNORECASE,
    )

    # Remove www
    if domain.startswith("www."):
        domain = domain[4:]

    # Remove path/query/fragment
    domain = re.split(
        r"[/?#]",
        domain,
        maxsplit=1,
    )[0]

    # Remove trailing dot
    domain = domain.rstrip(".")

    return domain


# ============================================================
# TLD EXTRACTION
# ============================================================

def extract_root_and_tld(domain):
    """
    Longest matching TLD wins.

    Example:
        brand.co.in
        -> brand + .co.in
    """

    for tld in sorted(
        TLD_SCORES.keys(),
        key=len,
        reverse=True,
    ):

        if domain.endswith(tld):

            root = domain[:-len(tld)]

            if root.endswith("."):
                root = root[:-1]

            # Must contain exactly one root label
            if "." in root:
                return None, None

            return root, tld

    return None, None


# ============================================================
# ROOT VALIDATION
# ============================================================

def validate_root(root):
    """
    Strict label validation.
    """

    if not root:
        return False

    if not (
        MIN_ROOT_LENGTH
        <= len(root)
        <= MAX_ROOT_LENGTH
    ):
        return False

    # Only a-z, 0-9 and optional single hyphen
    if not VALID_DOMAIN_PATTERN.fullmatch(root):
        return False

    # Hyphen cannot be first/last
    if root.startswith("-") or root.endswith("-"):
        return False

    # Maximum one hyphen
    if root.count("-") > 1:
        return False

    # Number-heavy names are usually weaker.
    digit_count = sum(
        char.isdigit()
        for char in root
    )

    if digit_count > 3:
        return False

    return True


# ============================================================
# TOKENIZATION
# ============================================================

def tokenize(root):
    """
    Split explicit domain separators.

    Example:
        glow-beauty
        -> ["glow", "beauty"]
    """

    return [
        token
        for token in re.split(
            r"[-_]",
            root,
        )
        if token
    ]


# ============================================================
# KEYWORD SCORING
# ============================================================

def calculate_keyword_score(root):
    """
    Returns:
        score
        matched_keywords
        categories

    Rules:
        - short keyword <= 3 chars:
          exact token only
        - longer keyword:
          exact token OR compound substring
        - multiple commercial terms receive bonus
    """

    tokens = tokenize(root)

    score = 0

    matched_keywords = set()
    matched_categories = set()

    # Prevent duplicate keyword scoring
    already_counted = set()

    for category, category_words in KEYWORDS.items():

        for keyword, points in category_words.items():

            if keyword in already_counted:
                continue

            # ------------------------------------------------
            # Short keywords
            # ------------------------------------------------

            if keyword in SHORT_KEYWORDS:

                if keyword in tokens:

                    score += points
                    matched_keywords.add(keyword)
                    matched_categories.add(category)
                    already_counted.add(keyword)

                continue

            # ------------------------------------------------
            # Exact token
            # ------------------------------------------------

            if keyword in tokens:

                score += points
                matched_keywords.add(keyword)
                matched_categories.add(category)
                already_counted.add(keyword)

                continue

            # ------------------------------------------------
            # Longer compound match
            # ------------------------------------------------

            if len(keyword) >= 4 and keyword in root:

                # Compound match receives slightly reduced value.
                adjusted_points = max(
                    4,
                    points - 4,
                )

                score += adjusted_points

                matched_keywords.add(keyword)
                matched_categories.add(category)
                already_counted.add(keyword)

    # Multiple relevant signals are useful.
    if len(matched_keywords) >= 2:
        score += 8

    # Multiple categories suggest broader commercial utility.
    if len(matched_categories) >= 2:
        score += 4

    return (
        score,
        sorted(matched_keywords),
        sorted(matched_categories),
    )


# ============================================================
# LENGTH SCORE
# ============================================================

def calculate_length_score(root):

    length = len(root)

    if length <= 4:
        return 30

    if length <= 6:
        return 25

    if length <= 8:
        return 20

    if length <= 10:
        return 15

    if length <= 12:
        return 10

    if length <= 14:
        return 5

    return -5


# ============================================================
# BRANDABILITY SCORE
# ============================================================

def calculate_brandability_score(root):

    score = 0

    length = len(root)

    # --------------------------------------------------------
    # Shorter is generally easier to brand.
    # --------------------------------------------------------

    if 4 <= length <= 6:
        score += 22

    elif 7 <= length <= 9:
        score += 18

    elif 10 <= length <= 12:
        score += 12

    elif 13 <= length <= 15:
        score += 5

    # --------------------------------------------------------
    # Vowel balance
    # --------------------------------------------------------

    vowels = sum(
        char in "aeiou"
        for char in root
    )

    vowel_ratio = vowels / max(
        1,
        length,
    )

    if 0.25 <= vowel_ratio <= 0.60:
        score += 10

    # --------------------------------------------------------
    # Repeated ugly characters
    # --------------------------------------------------------

    if REPEATED_CHAR_PATTERN.search(root):
        score -= 8

    # --------------------------------------------------------
    # Heavy consonant sequences
    # --------------------------------------------------------

    if HEAVY_CONSONANT_PATTERN.search(root):
        score -= 8

    # --------------------------------------------------------
    # Hyphen
    # --------------------------------------------------------

    if "-" in root:
        score -= 10

    # --------------------------------------------------------
    # Number penalty
    # --------------------------------------------------------

    digit_count = sum(
        char.isdigit()
        for char in root
    )

    if digit_count == 0:
        score += 8

    elif digit_count == 1:
        score += 2

    elif digit_count == 2:
        score -= 6

    else:
        score -= 12

    return score


# ============================================================
# COMMERCIAL INTENT
# ============================================================

def calculate_commercial_intent(
    root,
    matched_keywords,
):
    """
    Reward names that look useful for businesses/products.

    This is intentionally heuristic.
    """

    score = 0

    commercial_words = {
        "shop",
        "store",
        "brand",
        "market",
        "pay",
        "capital",
        "finance",
        "health",
        "clinic",
        "academy",
        "agency",
        "studio",
        "cloud",
        "saas",
        "software",
        "tech",
        "crypto",
        "beauty",
        "fashion",
        "food",
        "travel",
    }

    matched_commercial = (
        set(matched_keywords)
        & commercial_words
    )

    if matched_commercial:
        score += min(
            15,
            len(matched_commercial) * 5,
        )

    # Two-word-looking compounds can be commercially strong.
    # We only infer this when a hyphen explicitly separates them.
    if "-" in root:
        parts = [
            x
            for x in root.split("-")
            if x
        ]

        if len(parts) == 2:
            score += 5

    return score


# ============================================================
# QUALITY PENALTIES
# ============================================================

def calculate_quality_penalty(root):

    penalty = 0

    lower_root = root.lower()

    # --------------------------------------------------------
    # Weak prefix
    # --------------------------------------------------------

    for prefix in WEAK_PREFIXES:

        if (
            lower_root.startswith(prefix)
            and len(lower_root)
            > len(prefix) + 3
        ):
            penalty += 5
            break

    # --------------------------------------------------------
    # Repeated characters
    # --------------------------------------------------------

    if REPEATED_CHAR_PATTERN.search(
        lower_root
    ):
        penalty += 5

    # --------------------------------------------------------
    # Number at beginning/end
    # --------------------------------------------------------

    if lower_root[0].isdigit():
        penalty += 8

    if lower_root[-1].isdigit():
        penalty += 5

    # --------------------------------------------------------
    # Too many consonants
    # --------------------------------------------------------

    if HEAVY_CONSONANT_PATTERN.search(
        lower_root
    ):
        penalty += 5

    return penalty


# ============================================================
# FULL DOMAIN SCORE
# ============================================================

def score_domain(domain):
    """
    Calculate complete lead score.

    Returns dictionary containing internal ranking data.
    """

    normalized = normalize_domain(
        domain
    )

    if not normalized:
        return None

    root, tld = extract_root_and_tld(
        normalized
    )

    if not root or not tld:
        return None

    if not validate_root(root):
        return None

    # --------------------------------------------------------
    # TLD
    # --------------------------------------------------------

    tld_score = TLD_SCORES.get(
        tld,
        0,
    )

    # --------------------------------------------------------
    # Length
    # --------------------------------------------------------

    length_score = calculate_length_score(
        root
    )

    # --------------------------------------------------------
    # Keywords
    # --------------------------------------------------------

    (
        keyword_score,
        keywords,
        categories,
    ) = calculate_keyword_score(
        root
    )

    # --------------------------------------------------------
    # Brandability
    # --------------------------------------------------------

    brandability_score = (
        calculate_brandability_score(
            root
        )
    )

    # --------------------------------------------------------
    # Commercial intent
    # --------------------------------------------------------

    commercial_score = (
        calculate_commercial_intent(
            root,
            keywords,
        )
    )

    # --------------------------------------------------------
    # Quality penalties
    # --------------------------------------------------------

    penalty = calculate_quality_penalty(
        root
    )

    # --------------------------------------------------------
    # FINAL SCORE
    # --------------------------------------------------------

    final_score = (
        tld_score
        + length_score
        + keyword_score
        + brandability_score
        + commercial_score
        - penalty
    )

    # --------------------------------------------------------
    # Rating
    # --------------------------------------------------------

    if final_score >= 100:
        rating = "HOT"

    elif final_score >= 85:
        rating = "PREMIUM"

    elif final_score >= 70:
        rating = "STRONG"

    elif final_score >= MIN_PREMIUM_SCORE:
        rating = "REVIEW"

    else:
        rating = "REJECT"

    return {
        "domain": normalized,
        "root": root,
        "tld": tld,
        "score": final_score,
        "rating": rating,
        "keywords": keywords,
        "categories": categories,
        "tld_score": tld_score,
        "length_score": length_score,
        "keyword_score": keyword_score,
        "brandability_score": brandability_score,
        "commercial_score": commercial_score,
        "penalty": penalty,
    }


# ============================================================
# ATOMIC FILE WRITE
# ============================================================

def atomic_write_lines(
    filepath,
    lines,
):
    """
    Write output safely.

    Existing file isn't directly overwritten.
    """

    temp_path = filepath + ".tmp"

    try:

        with open(
            temp_path,
            "w",
            encoding="utf-8",
        ) as file:

            for line in lines:
                file.write(line)

        os.replace(
            temp_path,
            filepath,
        )

    except Exception:

        if os.path.exists(
            temp_path
        ):
            try:
                os.remove(
                    temp_path
                )
            except OSError:
                pass

        raise


# ============================================================
# MAIN
# ============================================================

def main():

    start_time = time.perf_counter()

    print()
    print("=" * 72)
    print("💎 BAWA DOMAIN FILTER v3.0")
    print("=" * 72)
    print()

    if not os.path.exists(
        INPUT_FILE
    ):

        print(
            f"❌ ERROR: '{INPUT_FILE}' "
            f"file nahi mili."
        )

        return 1

    # --------------------------------------------------------
    # Counters
    # --------------------------------------------------------

    total_scanned = 0
    empty_lines = 0
    duplicates = 0
    invalid_domains = 0
    rejected_low_score = 0
    premium_count = 0

    # --------------------------------------------------------
    # Dedup
    # --------------------------------------------------------

    seen = set()

    # Store only qualified results.
    qualified = []

    # --------------------------------------------------------
    # Category statistics
    # --------------------------------------------------------

    category_counter = defaultdict(
        int
    )

    # --------------------------------------------------------
    # Read input
    # --------------------------------------------------------

    try:

        with open(
            INPUT_FILE,
            "r",
            encoding="utf-8",
            errors="ignore",
        ) as infile:

            for line in infile:

                total_scanned += 1

                raw = line.strip()

                if not raw:
                    empty_lines += 1
                    continue

                domain = normalize_domain(
                    raw
                )

                if not domain:
                    invalid_domains += 1
                    continue

                # ------------------------------------------------
                # Duplicate
                # ------------------------------------------------

                if domain in seen:

                    duplicates += 1
                    continue

                seen.add(domain)

                # ------------------------------------------------
                # Score
                # ------------------------------------------------

                result = score_domain(
                    domain
                )

                if not result:

                    invalid_domains += 1
                    continue

                # ------------------------------------------------
                # Threshold
                # ------------------------------------------------

                if (
                    result["score"]
                    < MIN_PREMIUM_SCORE
                ):

                    rejected_low_score += 1
                    continue

                qualified.append(
                    result
                )

                premium_count += 1

                # Category stats
                for category in result[
                    "categories"
                ]:

                    category_counter[
                        category
                    ] += 1

    except OSError as exc:

        print(
            f"❌ Could not read input: {exc}"
        )

        return 1

    # --------------------------------------------------------
    # Sort
    #
    # Highest score first.
    # For equal scores, shorter domains first.
    # --------------------------------------------------------

    qualified.sort(
        key=lambda item: (
            -item["score"],
            len(item["root"]),
            item["domain"],
        )
    )

    # --------------------------------------------------------
    # Optional output cap
    # --------------------------------------------------------

    if (
        MAX_OUTPUT_DOMAINS
        and len(qualified)
        > MAX_OUTPUT_DOMAINS
    ):

        qualified = qualified[
            :MAX_OUTPUT_DOMAINS
        ]

    # --------------------------------------------------------
    # Create downstream-compatible output.
    #
    # IMPORTANT:
    # Only raw domains.
    # No scores.
    # No separators.
    # No metadata.
    # --------------------------------------------------------

    output_lines = [
        item["domain"] + "\n"
        for item in qualified
    ]

    try:

        atomic_write_lines(
            OUTPUT_FILE,
            output_lines,
        )

    except OSError as exc:

        print(
            f"❌ Could not write "
            f"'{OUTPUT_FILE}': {exc}"
        )

        return 1

    # --------------------------------------------------------
    # Human-readable ranking report
    # --------------------------------------------------------

    report_lines = []

    report_lines.append(
        "BAWA DOMAIN FILTER v3.0 REPORT\n"
    )
    report_lines.append(
        "=" * 90 + "\n"
    )

    report_lines.append(
        f"Total scanned      : {total_scanned:,}\n"
    )

    report_lines.append(
        f"Duplicates removed : {duplicates:,}\n"
    )

    report_lines.append(
        f"Invalid domains    : {invalid_domains:,}\n"
    )

    report_lines.append(
        f"Low-score rejected : {rejected_low_score:,}\n"
    )

    report_lines.append(
        f"Premium selected   : {premium_count:,}\n"
    )

    report_lines.append(
        "\nTOP QUALIFIED DOMAINS\n"
    )

    report_lines.append(
        "-" * 90 + "\n"
    )

    report_lines.append(
        "RANK | SCORE | DOMAIN | RATING | TLD | CATEGORY | KEYWORDS\n"
    )

    report_lines.append(
        "-" * 90 + "\n"
    )

    for index, item in enumerate(
        qualified,
        start=1,
    ):

        category = (
            item["categories"][0]
            if item["categories"]
            else "Brandable"
        )

        keywords = (
            ",".join(
                item["keywords"]
            )
            if item["keywords"]
            else "-"
        )

        report_lines.append(
            f"{index:04d} | "
            f"{item['score']:03d} | "
            f"{item['domain']} | "
            f"{item['rating']} | "
            f"{item['tld']} | "
            f"{category} | "
            f"{keywords}\n"
        )

    try:

        atomic_write_lines(
            REPORT_FILE,
            report_lines,
        )

    except OSError as exc:

        print(
            f"⚠️ Report file write failed: {exc}"
        )

    # --------------------------------------------------------
    # Console summary
    # --------------------------------------------------------

    elapsed = (
        time.perf_counter()
        - start_time
    )

    print(
        f"📊 Domains scanned      : "
        f"{total_scanned:,}"
    )

    print(
        f"♻️ Duplicates removed   : "
        f"{duplicates:,}"
    )

    print(
        f"❌ Invalid domains      : "
        f"{invalid_domains:,}"
    )

    print(
        f"🗑️ Low-score rejected   : "
        f"{rejected_low_score:,}"
    )

    print(
        f"💎 Premium selected     : "
        f"{premium_count:,}"
    )

    print(
        f"📁 Pipeline output      : "
        f"{OUTPUT_FILE}"
    )

    print(
        f"📋 Ranking report       : "
        f"{REPORT_FILE}"
    )

    # --------------------------------------------------------
    # Category summary
    # --------------------------------------------------------

    if category_counter:

        print()
        print(
            "🏷️ TOP DETECTED CATEGORIES"
        )

        for category, count in sorted(
            category_counter.items(),
            key=lambda item: (
                -item[1],
                item[0],
            )
        )[:10]:

            print(
                f"   {category:<15} "
                f"{count:,}"
            )

    # --------------------------------------------------------
    # Top 20
    # --------------------------------------------------------

    if qualified:

        print()
        print(
            "🏆 TOP 20 QUALIFIED DOMAINS"
        )
        print(
            "-" * 72
        )

        for index, item in enumerate(
            qualified[:20],
            start=1,
        ):

            category = (
                item["categories"][0]
                if item["categories"]
                else "Brandable"
            )

            print(
                f"{index:02d}. "
                f"{item['domain']:<25} "
                f"{item['score']:>3}  "
                f"{item['rating']:<8} "
                f"{category}"
            )

    else:

        print()
        print(
            "⚠️ No domain crossed the premium threshold."
        )

    print()
    print(
        f"⏱️ Time taken: {elapsed:.2f}s"
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
