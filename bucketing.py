"""
Bucketing logic - groups seller notes into broad product categories.

Three kinds of notes, based on what info is present:
  - SKU only (e.g. "WTSN026-DNMSHT-07-LT WASH-M"): the category code
    (2nd dash segment, or 2nd+3rd combined for codes like SWEAT-JKT) is
    looked up in CATEGORY_MAP. Unmapped codes go into "Others".
  - Descriptive name only (e.g. "WATSON BONES SNEAKERS (COLOR) - Size L"):
    matched against our known category names (88 finalized categories:
    shoe lines, named apparel, generic categories, accessories) so e.g.
    "JORDAN BASKETBALL JERSEY" routes into the shared "Basketball Jersey"
    bucket rather than creating a one-off bucket for the literal name.
    Anything that looks like a real product but doesn't match a known
    category lands in "Others". Genuinely unparseable fragments (too
    short, a bare number, a stray size/qty leftover) go to NEEDS REVIEW.
  - Both name and SKU: "PRODUCT NAME (COLOR) - Size L - SKU: XXXXX".
    Bucketed by the descriptive name (broad category), SKU kept as a
    field for reference/lookup.

Every order produces exactly ONE row in exactly ONE bucket, no matter how
many line items its note contains:
  - All lines the SAME category -> one row in that category's bucket,
    with a compact "N items" summary that expands to full detail on click
  - Lines spanning 2+ DIFFERENT categories -> one row in Bundles, same
    click-to-expand behavior
  - A single clean line -> one simple row, as before
This matters beyond display - the "Ship all in this bucket" button
collects every order_id shown and ships each one once. A duplicated
order_id across two rows would mean two real label purchases for one
order, so one-row-per-order is enforced here, not just cosmetic.
"""

import re

OTHERS = "Others"
NEEDS_REVIEW = "\u26a0\ufe0f NEEDS REVIEW"
BUNDLES = "\U0001F4E6 BUNDLES (multi-item orders)"

# --- Legacy SKU-code format (unchanged) ---
CATEGORY_MAP = {
    "DNMSHT": "Denim Shorts",
    "BBJ": "Basketball Jersey",
    "BAJ": "Baseball Jersey",
    "POLO": "Rugby Polo",
    "TSHT": "Tshirt",
    "MINK": "Mink Jacket",
    "SFLNL": "Short Sleeved Flannel Shirt",
    "MOTO": "Moto Jacket",
    "LESHT": "Leather Shorts",
    "CSHT": "Cotton Shorts",
    "CUTSHT": "Cutoff Tshirt",
    "CRODNMSHT": "Crochet Denim Shorts",
    "CARPSHT": "Carpenter Shorts",
    "TFLNL": "Tshirt Flannel",
    "LTSHT": "Long Sleeved Tshirts",
    "HOOD": "Hoodie",
    "DNMWSHT": "Denim Work Shirt",
    "PUFF": "Puffer Jacket",
    "TAPE": "Tapestry Shorts",
    "CSWT": "Sweater",
    "SSHT": "Short Sleeved Button Down Shirts",
    "PWSHT": "Button Shirt",
    "PPNTS": "Pants",
    "SWEAT-JKT": "Sweatsuit Jacket",  # two-segment code - matched before plain SWEAT
    "SHT": "Shorts",
    "SWEAT": "Sweats",
}


def match_category_code(parts):
    """Try a combined 2-segment code first (e.g. SWEAT-JKT), then a plain
    single-segment code (e.g. BBJ). Returns the friendly category name."""
    if len(parts) >= 3:
        two_seg = f"{parts[1]}-{parts[2]}".strip().upper()
        if two_seg in CATEGORY_MAP:
            return CATEGORY_MAP[two_seg]
    one_seg = parts[1].strip().upper()
    return CATEGORY_MAP.get(one_seg, OTHERS)


# ─── Descriptive-name category matching (88-category master list) ────
#
# Built from product_categories_FINAL.xlsx. Checked in TIERS, not one
# flat pool - a shoe line name (e.g. "BONES", 5 letters) is often
# SHORTER than the generic word that follows it in the same note
# ("SNEAKERS", 8 letters). Pure longest-match-wins across all 88 at once
# would let "Sneakers" win over "Bones" - wrong. So each tier is checked
# in priority order, and only the longest match WITHIN that tier is
# used, before falling through to the next tier. Singular and plural
# forms are both listed explicitly per category for clarity, even where
# substring matching would technically catch the plural via the
# singular form alone.

SHOE_LINE_ALIASES = {
    "COURT CLASSIC 2":  "Court Classic 2",
    "COURT CLASSIC":    "Court Classic",
    "CHUNKY BONES":     "Chunky Bones",
    "CRYPT CRAWLER":    "Crypt Crawler",
    "VULTURE STRAP":    "Vulture Strap",
    "VULTURE V2":       "Vulture V2",
    "VULTURE":          "Vulture",
    "TRIPLE 7 RACER":   "Triple 7 Racer",
    "LIFESTYLE 01":     "Lifestyle 01",
    "SOUL SPRINTER":    "Soul Sprinter",
    "LYTE RUNNER":      "Lyte Runner",
    "NORTH STAR":       "North Star",
    "ARTIC FOX":        "Artic Fox",
    "ARCTIC FOX":       "Artic Fox",
    "K FLIP":           "K Flip",
    "K-FLIP":           "K Flip",
    "KFLIP":            "K Flip",
    "BONESTA":          "Bonesta",
    "BONEBA":           "Boneba",
    "BONES":            "Bones",
    "VULCAN/VULCANS":   "Vulcan/Vulcans",
    "VULCANS":          "Vulcan/Vulcans",
    "VULCAN":           "Vulcan/Vulcans",
    "INVADER":          "Invader",
    "ROCKSTAR":         "Rockstar",
    "SKELETOR 2":       "Skeletor 2",
    "SKELETOR":         "Skeletor",
    "TRAILMAX":         "Trailmax",
    "COSMOS":           "Cosmos",
    "RAIDER":           "Raider",
    "LOPRO/LOPROS":     "LoPro/LoPros",
    "LOPROS":           "LoPro/LoPros",
    "LOPRO":            "LoPro/LoPros",
    "CONCORD":          "Concord",
    "ANURAS":           "Anuras",
    "ANURA":            "Anuras",
    "7SVN7":            "7SVN7",
    "CYBER":            "Cyber",
}

APPAREL_ALIASES = {
    "BASKETBALL JERSEYS": "Basketball Jersey",
    "BASKETBALL JERSEY":  "Basketball Jersey",
    "BASEBALL JERSEYS":   "Baseball Jersey",
    "BASEBALL JERSEY":    "Baseball Jersey",
    "FOOTBALL JERSEYS":   "Football Jersey",
    "FOOTBALL JERSEY":    "Football Jersey",
    "HOCKEY JERSEYS":     "Hockey Jersey",
    "HOCKEY JERSEY":      "Hockey Jersey",
    "SOCCER JERSEYS":     "Soccer Jersey",
    "SOCCER JERSEY":      "Soccer Jersey",
    "MOVIE JERSEYS":      "Movie Jersey",
    "MOVIE JERSEY":       "Movie Jersey",
    "JERSEY DRESSES":     "Jersey Dress",
    "JERSEY DRESS":       "Jersey Dress",
    "BASKETBALL SHORTS":  "Basketball Shorts",
    "BASKETBALL SHORT":   "Basketball Shorts",
    "SATIN JACKETS":      "Satin Jacket",
    "SATIN JACKET":       "Satin Jacket",
    "VARSITY JACKETS":    "Varsity Jacket",
    "VARSITY JACKET":     "Varsity Jacket",
    "MOTO JACKETS":       "Moto Jacket",
    "MOTO JACKET":        "Moto Jacket",
    "RACING JACKETS":     "Racing Jackets",
    "RACING JACKET":      "Racing Jackets",
    "MINK JACKETS":       "Mink Jacket",
    "MINK JACKET":        "Mink Jacket",
    "DICKIES JACKETS":    "Dickies Jacket",
    "DICKIES JACKET":     "Dickies Jacket",
    "LEATHER JACKETS":    "Leather Jacket",
    "LEATHER JACKET":     "Leather Jacket",
    "SHERPA JACKETS":     "Sherpa Jacket",
    "SHERPA JACKET":      "Sherpa Jacket",
    "PUFFER JACKETS":     "Puffer Jacket/Vest",
    "PUFFER JACKET":      "Puffer Jacket/Vest",
    "PUFFER VESTS":       "Puffer Jacket/Vest",
    "PUFFER VEST":        "Puffer Jacket/Vest",
    "DENIM JACKETS":      "Denim Jacket",
    "DENIM JACKET":       "Denim Jacket",
    "WORK JACKETS":       "Work Jacket",
    "WORK JACKET":        "Work Jacket",
    "FLANNEL SHIRTS":     "Flannel Shirt",
    "FLANNEL SHIRT":      "Flannel Shirt",
    "WORK SHIRTS":        "Work Shirt",
    "WORK SHIRT":         "Work Shirt",
    "SATIN PANTS":        "Satin Pants",
    "SATIN PANT":         "Satin Pants",
    "LONG SLEEVE TEES":   "Long Sleeve Tee",
    "LONG SLEEVE TEE":    "Long Sleeve Tee",
    "LONG SLEEVE TSHIRT": "Long Sleeve Tee",
    "SWEATSUITS":         "Sweatsuit",
    "SWEATSUIT":          "Sweatsuit",
}

ACCESSORY_ALIASES = {
    "PANT CHAINS":  "Denim Chain",
    "PANT CHAIN":   "Denim Chain",
    "DENIM CHAINS": "Denim Chain",
    "DENIM CHAIN":  "Denim Chain",
    "SNAPBACKS":    "Snapback",
    "SNAPBACK":     "Snapback",
    "STRAPBACKS":   "Snapback",
    "STRAPBACK":    "Snapback",
    "BACKPACKS":    "Backpack",
    "BACKPACK":     "Backpack",
    "BOOKBAGS":     "Backpack",
    "BOOKBAG":      "Backpack",
    "DUFFLE BAGS":  "Duffle Bag",
    "DUFFLE BAG":   "Duffle Bag",
    "DUFFLE":       "Duffle Bag",
    "LANYARDS":     "Lanyard",
    "LANYARD":      "Lanyard",
    "BEANIES":      "Beanie",
    "BEANIE":       "Beanie",
    "SOCKS":        "Socks",
    "SOCK":         "Socks",
    "PINS":         "Pin",
    "PIN":          "Pin",
    "RUGS":         "Rug",
    "RUG":          "Rug",
    "BELTS":        "Belts",
    "BELT":         "Belts",
    "FLAGS":        "Flag",
    "FLAG":         "Flag",
    "TRUCKER HAT":  "Hats",
    "DAD HAT":      "Hats",
    "DADHAT":       "Hats",
    "HATS":         "Hats",
    "HAT":          "Hats",
}

GENERIC_ALIASES = {
    "T-SHIRTS":   "Tshirts",
    "T-SHIRT":    "Tshirts",
    "TSHIRTS":    "Tshirts",
    "TSHIRT":     "Tshirts",
    "HOODIES":    "Hoodie",
    "HOODIE":     "Hoodie",
    "SWEATPANTS": "Sweatpants",
    "SWEATSHIRTS": "Sweatshirt",
    "SWEATSHIRT": "Sweatshirt",
    "SWEATERS":   "Sweater",
    "SWEATER":    "Sweater",
    "CROP TOPS":  "Crop Top",
    "CROP TOP":   "Crop Top",
    "POLOS":      "Polo",
    "POLO":       "Polo",
    "VESTS":      "Vest",
    "VEST":       "Vest",
    "SKIRTS":     "Skirt",
    "SKIRT":      "Skirt",
    "UNDERWEAR":  "Underwear",
    "BOXERS":     "Boxers",
    "BOXER":      "Boxers",
    "DENIM":      "Denim",
    "SHORTS":     "Shorts",
    "SHORT":      "Shorts",
    "PANTS":      "Pants",
    "PANT":       "Pants",
    "JERSEYS":    "Jerseys",
    "JERSEY":     "Jerseys",
    "SNEAKERS":   "Sneakers",
    "SNEAKER":    "Sneakers",
    "OUTERWEAR":  "Outerwear",
    "JACKETS":    "Jackets",
    "JACKET":     "Jackets",
    "SHIRTS":     "Shirt",
    "SHIRT":      "Shirt",
}

# Rock band names -> "Rock Tshirt" whenever the note also says TSHIRT.
# (These don't fit a simple substring-of-category-name rule since the
# category name "Rock Tshirt" itself never literally appears in a note.)
ROCK_BANDS = {
    "ACDC", "AC/DC", "AEROSMITH", "DEF LEPPARD", "KISS",
    "MOTLEY CRUE", "M\u00d6TLEY CR\u00dcE", "MOTLEY CR\u00dcE", "M\u00d6TLEY CRUE",
}


def match_known_category_name(name):
    """
    If a plain-text product name contains one of our known category names
    as a phrase (e.g. "JORDAN BASEBALL JERSEY" contains "BASEBALL JERSEY"),
    route it into that shared category bucket instead of creating a one-off
    bucket for the literal name.

    Checked tier by tier (shoe lines -> apparel -> accessories -> generic),
    with longest-match-wins WITHIN each tier - so "Court Classic 2" beats
    "Court Classic", but a shoe line like "Bones" still correctly beats the
    generic word "Sneakers" that usually follows it in the same note, since
    shoe lines are checked as a whole tier before generic words are even
    considered. Falls back to Rock Tshirt / Donation special cases, then
    None if nothing matches at all.
    """
    name_upper = name.upper()

    # Shoe lines and apparel first - both are "specific" tiers.
    for tier in (SHOE_LINE_ALIASES, APPAREL_ALIASES):
        best_match = None
        for alias in tier:
            if alias in name_upper:
                if best_match is None or len(alias) > len(best_match):
                    best_match = alias
        if best_match:
            return tier[best_match]

    # Rock Tshirt: band name + "TSHIRT" together. Checked at the same
    # priority as apparel (Rock Tshirt IS a Named Apparel category) -
    # must run BEFORE the generic tier below, otherwise generic "TSHIRT"
    # matches first and this check never gets a chance to run.
    if "TSHIRT" in name_upper or "T-SHIRT" in name_upper:
        for band in ROCK_BANDS:
            if band in name_upper:
                return "Rock Tshirt"

    # Accessories, then generic categories.
    for tier in (ACCESSORY_ALIASES, GENERIC_ALIASES):
        best_match = None
        for alias in tier:
            if alias in name_upper:
                if best_match is None or len(alias) > len(best_match):
                    best_match = alias
        if best_match:
            return tier[best_match]

    # Donation / charity items
    if "DONATION" in name_upper or "CHARITY" in name_upper:
        return "Donation"

    return None


SIZE_PATTERN = re.compile(r'[-\u2013\u2014]\s*Size\s+(\S+)$', re.IGNORECASE)
STANDARD_SIZE_TOKEN = re.compile(r'^(XXS|XS|S|M|L|XL|XXL|XXXL|2XL|3XL|4XL|\d{1,3}(\.\d+)?)$', re.IGNORECASE)
QTY_PREFIX_PATTERN = re.compile(r'^\(?\s*QTY?:?\s*\d+\s*\)?\s*', re.IGNORECASE)
SKU_PREFIX_PATTERN = re.compile(r'^[A-Z0-9]+-[A-Z]+-', re.IGNORECASE)
SKU_SUFFIX_PATTERN = re.compile(r'[-\u2013\u2014]\s*SKU:\s*(\S+)\s*$', re.IGNORECASE)

SIZE_WORD = r'(XXS|XS|S|M|L|XL|XXL|XXXL|2XL|3XL|4XL)'
ALL_SIZE_WORD = r'(XXS|XS|SMALL|S|MEDIUM|M|LARGE|L|X-?LARGE|XL|XX-?LARGE|XXL|2XL|XXX-?LARGE|3XL|4XL)'
SIZE_WORD_NORMALIZE = {
    "SMALL": "S", "MEDIUM": "M", "LARGE": "L",
    "XLARGE": "XL", "X-LARGE": "XL",
    "XXLARGE": "XXL", "XX-LARGE": "XXL",
    "XXXLARGE": "3XL", "XXX-LARGE": "3XL",
}
ALL_SIZE_LINE = re.compile(rf'^ALL\s+{ALL_SIZE_WORD}$', re.IGNORECASE)
QTY_SIZE_LINE = re.compile(rf'^(\d+)\s*{SIZE_WORD}$', re.IGNORECASE)
BARE_NUMBER = re.compile(r'^\d+$')


def split_multi_item_note(raw_note):
    """A single order's seller_note can contain multiple lines (one per line
    item, or one per size in a size-breakdown), separated by our ' | '
    flattening or a raw newline from the API."""
    parts = re.split(r'\s*\|\s*|\r?\n', raw_note or "")
    return [p.strip() for p in parts if p.strip()]


def extract_size(note):
    """Pull the size off a single note line. Only returns a value we're
    confident is actually a size (not a color) - returns '' otherwise."""
    note = note.strip()
    m = SIZE_PATTERN.search(note)
    if m:
        return m.group(1)
    parts = note.split('-')
    if len(parts) >= 2:
        last = parts[-1].strip()
        if STANDARD_SIZE_TOKEN.match(last):
            return last
    return ""


def extract_color(note):
    """Pull the color/variant off a single note line, when present."""
    note = note.strip()
    note = QTY_PREFIX_PATTERN.sub('', note).strip()

    if '(' in note and ')' in note:
        m = re.search(r'\(([^)]+)\)', note)
        if m:
            return m.group(1).strip().upper()
        return ""

    parts = note.split('-')
    if extract_size(note) and len(parts) >= 3:
        candidate = parts[-2].strip()
        if candidate and not candidate.isdigit():  # a bare number is a batch code, not a color
            return candidate.upper()

    return ""


def is_junk_key(key):
    key = key.strip()
    if len(key) < 3:
        return True
    if BARE_NUMBER.match(key):
        return True
    if QTY_SIZE_LINE.match(key):
        return True
    if ALL_SIZE_LINE.match(key):
        return True
    return False


def parse_single_line(line):
    """
    Parse one note line into a dict: {bucket_key, name, sku, color, size, is_descriptive}

    If the line ends with "- SKU: XXXX", that's peeled off first and used to
    populate the sku field - everything before it is then parsed exactly as
    it always has been (so this is purely additive to the existing formats).
    """
    explicit_sku = ""
    suffix_match = SKU_SUFFIX_PATTERN.search(line)
    if suffix_match:
        explicit_sku = suffix_match.group(1).strip()
        line = SKU_SUFFIX_PATTERN.sub('', line).strip()

    result = _parse_single_line_core(line)
    if explicit_sku:
        result["sku"] = explicit_sku
    return result


def _parse_single_line_core(line):
    stripped = QTY_PREFIX_PATTERN.sub('', line.strip()).strip()

    if SKU_PREFIX_PATTERN.match(stripped):
        # SKU style: "WTSN026-DNMSHT-07-LT WASH-M" - check this FIRST, since a
        # trailing annotation like "(3 JERSEY)" can otherwise look descriptive.
        parts = stripped.split('-')
        category_name = match_category_code(parts)
        color = extract_color(line)
        size = extract_size(line)
        clean_sku = SIZE_PATTERN.sub('', stripped).strip()  # drop redundant "- Size X" suffix
        return {
            "bucket_key": category_name,
            "name": "",
            "sku": clean_sku,
            "color": color,
            "size": size,
            "is_descriptive": False,
        }

    if '(' in stripped:
        # Descriptive style: "WATSON BONES SNEAKERS (BUMBLEBEE) - Size 11.5"
        base = SIZE_PATTERN.sub('', stripped).strip()
        name = base.split('(')[0].strip().upper()
        color = extract_color(line)
        size = extract_size(line)
        bucket_key = match_known_category_name(name) or OTHERS
        return {
            "bucket_key": bucket_key,
            "name": name,
            "sku": "",
            "color": color,
            "size": size,
            "is_descriptive": True,
        }

    # Plain-language name with a size suffix but no color, e.g.
    # "BLACK LEATHER CROSS SHORTS - Size 38" - has spaces, so it's words,
    # not a SKU code, even though it contains dashes.
    size_match = SIZE_PATTERN.search(stripped)
    if size_match:
        name_part = SIZE_PATTERN.sub('', stripped).strip()
        if ' ' in name_part:
            name_upper = name_part.upper()
            bucket_key = match_known_category_name(name_upper) or OTHERS
            return {
                "bucket_key": bucket_key,
                "name": name_upper,
                "sku": "",
                "color": "",
                "size": size_match.group(1),
                "is_descriptive": True,
            }

    # Bare descriptive name - no parens, no recognizable "- Size X" suffix,
    # but still plain language (has a space) rather than a SKU code, e.g.
    # "WOMEN'S RHINESTONE BELT" or "BLACK BOOST PIN". Very common for
    # accessories, which often don't carry size/color info in the note at
    # all. Try the full known-category match before giving up. A real match
    # returns that category; no match still returns Others (not a one-off
    # literal-name bucket) - is_junk_key() downstream is what decides if
    # this should actually be NEEDS REVIEW instead (too short, a bare
    # number, a stray fragment), independent of category matching.
    if ' ' in stripped:
        bucket_key = match_known_category_name(stripped.upper()) or OTHERS
        return {
            "bucket_key": bucket_key,
            "name": stripped.upper(),
            "sku": "",
            "color": extract_color(line),
            "size": extract_size(line),
            "is_descriptive": True,
        }

    # Last-resort SKU-style fallback - only for genuinely code-like text
    # (no spaces anywhere), since real SKUs were already caught above.
    parts = stripped.split('-')
    if len(parts) >= 2 and ' ' not in stripped:
        category_name = match_category_code(parts)
        color = extract_color(line)
        size = extract_size(line)
        clean_sku = SIZE_PATTERN.sub('', stripped).strip()
        return {
            "bucket_key": category_name,
            "name": "",
            "sku": clean_sku,
            "color": color,
            "size": size,
            "is_descriptive": False,
        }

    # Neither format recognized - single word, no dash, no space
    return {
        "bucket_key": NEEDS_REVIEW,
        "name": "",
        "sku": "",
        "color": "",
        "size": "",
        "is_descriptive": False,
    }


def parse_note_lines(lines):
    """
    Walk a note's lines in order, resolving "ALL 3XL" size declarations and
    "2 M" orphan size/qty breakdowns (see parse_single_line for the base
    per-line parsing). Returns a list of entries, one per unit.
    """
    entries = []
    pending_size = None
    last_parsed = None

    for line in lines:
        all_size_match = ALL_SIZE_LINE.match(line)
        if all_size_match:
            raw_size = all_size_match.group(1).upper().replace('-', '')
            pending_size = SIZE_WORD_NORMALIZE.get(raw_size, raw_size)
            continue

        qty_size_match = QTY_SIZE_LINE.match(line)
        if qty_size_match and last_parsed:
            qty = int(qty_size_match.group(1))
            size = qty_size_match.group(2).upper()
            entry = dict(last_parsed)
            entry["size"] = size
            entry["qty"] = qty
            entry["raw"] = line
            entries.append(entry)
            continue

        parsed = parse_single_line(line)
        if is_junk_key(parsed["bucket_key"]):
            parsed["bucket_key"] = NEEDS_REVIEW

        if not parsed["size"] and pending_size:
            parsed["size"] = pending_size

        parsed["qty"] = 1
        parsed["raw"] = line
        entries.append(parsed)

        if parsed["bucket_key"] != NEEDS_REVIEW:
            last_parsed = parsed

    return entries


def bucket_orders(orders):
    """
    Takes a list of order dicts (from Get Order Detail) and returns a dict of
    bucket_key -> list of item dicts.

    Every order produces exactly ONE row in its bucket, no matter how many
    lines its note contained:
      - Lines spanning 2+ DIFFERENT CATEGORIES -> one row in Bundles
      - All lines in the SAME category (regardless of how many lines, or
        differences in size/color/SKU within that category) -> one row in
        that category, with a compact "N items" summary that expands to
        full detail on click
      - A single clean line -> one simple row, as before
      - All lines unparseable -> one row in Needs Review

    This is enforced for real: the "Ship all in this bucket" button in the
    dashboard collects every order_id shown in a bucket and ships each one
    once. A duplicated order_id across two rows would mean two separate
    label purchases for the same order.
    """
    buckets = {}

    for o in orders:
        raw_note = o.get("seller_note", "")
        lines = split_multi_item_note(raw_note)
        if not lines:
            continue

        entries = parse_note_lines(lines)
        if not entries:
            continue

        real_entries = [e for e in entries if e["bucket_key"] != NEEDS_REVIEW]
        distinct_categories = set(e["bucket_key"] for e in real_entries)

        if not real_entries:
            target_bucket = NEEDS_REVIEW
            display_entries = entries
        elif len(distinct_categories) >= 2:
            target_bucket = BUNDLES
            display_entries = entries
        else:
            target_bucket = real_entries[0]["bucket_key"]
            # A line like "PRODUCT - 10 JERSEYS" with no size of its own is
            # just a header identifying the product - drop it once real
            # sized breakdown lines exist, so totals aren't inflated.
            sized = [e for e in real_entries if e["size"]]
            display_entries = sized if sized else real_entries

        total_units = sum(e["qty"] for e in display_entries)

        if len(display_entries) == 1 and display_entries[0]["qty"] == 1:
            # Simple case - one clean line, shown directly
            e = display_entries[0]
            item = {
                "order_id": o.get("id", ""),
                "name": e["name"],
                "sku": e["sku"],
                "color": e["color"],
                "size": e["size"],
                "full_note": raw_note,
                "create_time": o.get("create_time", ""),
                "total_amount": o.get("payment", {}).get("total_amount", ""),
            }
        else:
            # Multiple lines for this order in this bucket - ONE row,
            # expandable, so "Ship all" only ever sees this order_id once.
            item = {
                "order_id": o.get("id", ""),
                "items_detail": [
                    {
                        "line": e["raw"], "name": e["name"], "sku": e["sku"],
                        "color": e["color"], "size": e["size"], "qty": e["qty"],
                    }
                    for e in display_entries
                ],
                "total_units": total_units,
                "full_note": raw_note,
                "create_time": o.get("create_time", ""),
                "total_amount": o.get("payment", {}).get("total_amount", ""),
            }

        buckets.setdefault(target_bucket, []).append(item)

    return buckets