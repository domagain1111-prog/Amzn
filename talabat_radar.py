import os
import re
import json
import time
import asyncio
import urllib.parse
from collections import deque
import requests
from playwright.async_api import async_playwright

# ================= الإعدادات والمتغيرات البيئية =================
TELEGRAM_TOKEN = (os.environ.get("TALABAT_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_TOKEN") or "").strip()
CHAT_ID = (os.environ.get("TALABAT_CHAT_ID") or os.environ.get("CHAT_ID") or "").strip()
TALABAT_COOKIES = os.environ.get("TALABAT_COOKIES", "").strip()

LATITUDE = float(os.environ.get("TALABAT_LAT") or "31.2156")
LONGITUDE = float(os.environ.get("TALABAT_LNG") or "29.9553")

STORE_BASE_URL = (os.environ.get("TALABAT_MART_URL") or "https://www.talabat.com/egypt/grocery/650267/tmart-new-maadi-taqseem-laselky").rstrip("/")

SEED_URLS = [
    ("الصفحة الرئيسية لطلبات مارت", STORE_BASE_URL),
    ("عروض الموسم", f"{STORE_BASE_URL}/fruit-veg/best-in-season"),
]

MIN_DISCOUNT_PERCENT = 25      # الحد الأدنى للخصم الفعلي
FLASH_DISCOUNT_PERCENT = 50    # الحد الأدنى للتنبيه الناري
MAX_REALISTIC_DISCOUNT = 85    # استبعاد أي نسبة غير منطقية
CACHE_TTL_DAYS = 7
PRICE_HISTORY_TTL_DAYS = 21
COOKIE_ALERT_COOLDOWN = 12 * 3600
STATE_FILE = "sent_talabat.json"
MAX_URLS_LIMIT = 260
CONCURRENT_TABS = 5
MAX_PAGES_PER_CATEGORY = 6

# ================= أنماط عروض 1+1 والعروض المشروطة (مثل اشتري بـ 200 وخذ 50%) =================
BOGO_REGEX = re.compile(
    r"(?:\b1\s*\+\s*1\b|\b2\s*for\s*1\b|\bbuy\s*1\s*get\s*1\b|\bbogo\b|واحد\s*\+\s*واحد|قطعة\s*\+\s*قطعة)",
    flags=re.IGNORECASE,
)

SPEND_PROMO_REGEX = re.compile(
    r"(?:spend|buy|اشتر[يِ]?|اطلب)\s*(?:for\s*)?(?:egp\s*)?\d+(?:\.\d+)?(?:\s*(?:egp|ج\.م|جنيه))?\s*(?:and\s*)?(?:get|save|خد|خذ|احصل\s*على|وفر|خصم)",
    flags=re.IGNORECASE,
)

PROMO_TEXT_KEYS = {
    "promotion", "promotiontext", "promo", "promotext", "badge", "badgetext",
    "offer", "offertext", "offertag", "dealtext", "tag", "tags", "label",
    "subtitle", "description", "shortdescription", "marketingtext", "bundletext",
    "conditions", "condition", "discountlabel", "discounttext"
}

# ================= المستوى الأول: أقسام العروض والخصومات =================
DEALS_PRIORITY_CATEGORIES = [
    "offer", "deal", "promo", "save", "discount", "clearance", "bundle", "value", "hot",
    "عروض", "تخفيض", "خصم", "تصفية", "توفير", "best-in-season"
]

# ================= المستوى الثاني: أقسام السوبرماركت وخزين البيت =================
SUPERMARKET_PRIORITY_CATEGORIES = [
    "cooking", "baking", "pantry", "cupboard", "rice", "pasta", "oil", "ghee", "sugar", "canned", "sauce",
    "poultry", "meat", "beef", "seafood", "fish", "deli", "cold-cuts",
    "dairy", "cheese", "milk", "egg", "yogurt", "butter",
    "chocolate", "biscuit", "snack", "chips", "sweet", "candy", "nuts",
    "cleaning", "laundry", "detergent", "household", "paper", "tissue",
    "coffee", "tea", "beverage", "juice", "water"
]

# ================= قائمة استبعاد الأقسام غير المهمة =================
IGNORED_CATEGORIES = [
    "pet", "dog", "cat", "حيوانات", "قطط", "كلاب",
    "baby", "diaper", "infant", "أطفال", "حفاضات", "رضع", "formula",
    "stationery", "office", "مكتبية", "أدوات مدرسية", "school",
    "tobacco", "cigarette", "vape", "سجائر", "تبغ", "lighter",
    "party", "gift", "هدايا", "حفلات", "flower", "bouquet", "ورد", "زهور",
    "toy", "game", "ألعاب", "candle", "شموع", "accessory", "cable", "charger"
]


# ================= إدارة الكاش وسجل الأسعار التاريخي =================
def load_and_clean_sent():
    now = int(time.time())
    cutoff = now - (CACHE_TTL_DAYS * 86400)
    hist_cutoff = now - (PRICE_HISTORY_TTL_DAYS * 86400)
    data = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
                if isinstance(raw, dict):
                    data = raw
        except Exception:
            pass

    cleaned = {}
    price_history = {}

    raw_hist = data.get("__PRICE_HISTORY__")
    if isinstance(raw_hist, dict):
        for hk, hv in raw_hist.items():
            if isinstance(hv, dict) and hv.get("ts", 0) >= hist_cutoff and hv.get("p", 0) > 0:
                price_history[hk] = hv

    for k, v in data.items():
        if k == "__PRICE_HISTORY__":
            continue
        if k == "__ALERT_TS__":
            if isinstance(v, (int, float)) and (now - v) < 86400:
                cleaned[k] = v
            continue
        if isinstance(v, dict):
            if v.get("ts", 0) >= cutoff and ("price" in v or "promo" in v):
                cleaned[k] = v
        elif isinstance(v, (int, float)):
            cleaned[k] = {"price": v, "ts": now}

    cleaned["__PRICE_HISTORY__"] = price_history
    return cleaned


def get_saved_price(sent_deals, key):
    val = sent_deals.get(key)
    if isinstance(val, dict):
        return val.get("price")
    if isinstance(val, (int, float)):
        return val
    return None


def get_saved_promo(sent_deals, key):
    val = sent_deals.get(key)
    if isinstance(val, dict):
        return val.get("promo")
    return None


def record_deal(sent_deals, key, price, promo=None):
    entry = {"price": price, "ts": int(time.time())}
    if promo:
        entry["promo"] = promo
    sent_deals[key] = entry


def save_sent(data):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ================= بناء روابط البحث والمتصفح =================
def build_best_product_url(item, category_url):
    title = item.get("title", "")
    clean_words = re.sub(r"[\+\-\(\)\[\],/\\\|×\*]+", " ", title)
    short_query = " ".join(clean_words.split()[:4])
    encoded_q = urllib.parse.quote(short_query)
    search_url = f"https://www.talabat.com/egypt/talabat-mart/search?q={encoded_q}"

    slug = (item.get("slug") or "").strip()
    prod_id = str(item.get("prodId") or "").strip()
    if slug and not slug.startswith("http") and not slug.startswith("/"):
        if prod_id and prod_id not in slug:
            web_url = f"{STORE_BASE_URL}/product/{slug}/{prod_id}"
        else:
            web_url = f"{STORE_BASE_URL}/product/{slug}"
    else:
        web_url = category_url

    return search_url, web_url


# ================= حساب سعر الكيلو أو اللتر =================
def calculate_unit_price_info(title, price):
    if not title or price <= 0:
        return ""
    ar_digits = "٠١٢٣٤٥٦٧٨٩"
    en_digits = "0123456789"
    clean = title.translate(str.maketrans(ar_digits, en_digits))

    pattern = r"(\d+(?:\.\d+)?)\s*(kg|g|gm|gram|grams|l|ltr|liter|ml|كيلو|كجم|جم|جرام|لتر|مل|مللي)\b(?:\s*[xX\*×]\s*(\d+))?"
    match = re.search(pattern, clean, flags=re.IGNORECASE)
    if not match:
        return ""

    val = float(match.group(1))
    unit_raw = match.group(2).lower()
    multiplier = int(match.group(3)) if match.group(3) else 1

    if multiplier == 1:
        pre_match = re.search(r"(\d+)\s*[xX\*×]\s*" + re.escape(match.group(1)), clean)
        if pre_match:
            multiplier = int(pre_match.group(1))

    total_val = val * multiplier
    if total_val <= 0:
        return ""

    if unit_raw in ("kg", "كيلو", "كجم"):
        kg_amount, unit_label = total_val, "الكيلو"
    elif unit_raw in ("g", "gm", "gram", "grams", "جم", "جرام"):
        kg_amount, unit_label = total_val / 1000.0, "الكيلو"
    elif unit_raw in ("l", "ltr", "liter", "لتر"):
        kg_amount, unit_label = total_val, "اللتر"
    elif unit_raw in ("ml", "مل", "مللي"):
        kg_amount, unit_label = total_val / 1000.0, "اللتر"
    else:
        return ""

    if kg_amount < 0.05:
        return ""

    unit_price = round(price / kg_amount, 2)
    return f"⚖️ السعر الصافي لـ{unit_label}: {unit_price} EGP"


# ================= إرسال تنبيهات تليجرام =================
def send_telegram_text(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    sent_ok = False
    for cid in [c.strip() for c in CHAT_ID.split(",") if c.strip()]:
        try:
            r = requests.post(url, json={"chat_id": cid, "text": text}, timeout=12)
            if r.status_code == 200:
                sent_ok = True
        except Exception:
            pass
    return sent_ok


def send_telegram_deal(caption, img_url="", buttons=None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False

    photo_api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    msg_api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    reply_markup = {"inline_keyboard": buttons} if buttons else None
    clean_img = img_url.split("?")[0] if img_url else ""

    sent_ok = False
    for cid in [c.strip() for c in CHAT_ID.split(",") if c.strip()]:
        delivered = False
        if clean_img and clean_img.startswith("http"):
            try:
                payload = {"chat_id": cid, "photo": clean_img, "caption": caption[:1020], "parse_mode": "Markdown"}
                if reply_markup:
                    payload["reply_markup"] = reply_markup
                r = requests.post(photo_api, json=payload, timeout=12)
                if r.status_code == 200:
                    delivered, sent_ok = True, True
            except Exception:
                pass

        if not delivered:
            try:
                payload = {"chat_id": cid, "text": caption, "parse_mode": "Markdown", "disable_web_page_preview": False}
                if reply_markup:
                    payload["reply_markup"] = reply_markup
                r = requests.post(msg_api, json=payload, timeout=12)
                if r.status_code == 200:
                    sent_ok = True
            except Exception:
                pass
    return sent_ok


def maybe_send_alert(sent_deals, reason):
    now = int(time.time())
    last_ts = sent_deals.get("__ALERT_TS__", 0)
    if (now - last_ts) >= COOKIE_ALERT_COOLDOWN:
        alert_msg = (
            "⚠️ تنبيه من رادار Talabat Mart!\n\n"
            f"السبب: {reason}\n"
            "📍 راجع صلاحية TALABAT_COOKIES أو TALABAT_MART_URL."
        )
        if send_telegram_text(alert_msg):
            sent_deals["__ALERT_TS__"] = now


# ================= تجهيز الكوكيز والجلسة =================
def parse_playwright_cookies(raw_json):
    if not raw_json:
        return []
    try:
        raw_list = json.loads(raw_json)
    except Exception:
        print("⚠️ صيغة TALABAT_COOKIES غير صحيحة!")
        return []

    now_sec = int(time.time())
    future_exp = now_sec + (180 * 86400)
    cleaned = []
    for c in raw_list:
        if not isinstance(c, dict) or not c.get("name") or c.get("value") is None:
            continue
        domain = str(c.get("domain", ".talabat.com"))
        if "talabat.com" not in domain:
            continue
        item = {
            "name": str(c["name"]),
            "value": str(c["value"]),
            "domain": domain,
            "path": c.get("path", "/"),
            "secure": bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", False)),
            "expires": future_exp
        }
        ss = str(c.get("sameSite", "")).lower()
        if ss == "strict":
            item["sameSite"] = "Strict"
        elif ss == "lax":
            item["sameSite"] = "Lax"
        elif ss in ("none", "no_restriction"):
            item["sameSite"] = "None"
            item["secure"] = True
        cleaned.append(item)
    return cleaned


def normalize_title(title):
    return re.sub(r"\s+", " ", str(title)).strip()


# ================= تصنيف عروض 1+1 والعروض المشروطة =================
def classify_special_promo(texts):
    cleaned_texts = []
    for t in texts:
        if not t:
            continue
        nt = normalize_title(t)
        if 3 <= len(nt) <= 220 and nt not in cleaned_texts:
            cleaned_texts.append(nt)

    for nt in cleaned_texts:
        if BOGO_REGEX.search(nt):
            return "bogo", nt

    for nt in cleaned_texts:
        if SPEND_PROMO_REGEX.search(nt):
            return "spend_promo", nt

    return None, ""


def collect_promo_strings(obj, out_list, depth=0):
    if depth > 4 or obj is None:
        return
    if isinstance(obj, str):
        s = normalize_title(obj)
        if 3 <= len(s) <= 200 and s not in out_list:
            out_list.append(s)
    elif isinstance(obj, list):
        for el in obj[:15]:
            collect_promo_strings(el, out_list, depth + 1)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if any(pk in kl for pk in PROMO_TEXT_KEYS):
                collect_promo_strings(v, out_list, depth + 1)


# ================= استخراج المنتجات وعروض 1+1 والمشروطة من __NEXT_DATA__ =================
def extract_products_from_next_data(obj, results, meta):
    if isinstance(obj, dict):
        for pg_key in ("totalPages", "pageCount", "lastPage", "total_pages"):
            if pg_key in obj and isinstance(obj[pg_key], int) and 1 <= obj[pg_key] <= 15:
                if obj[pg_key] > meta["total_pages"]:
                    meta["total_pages"] = obj[pg_key]

        name = obj.get("name") or obj.get("title") or obj.get("productName")
        price = obj.get("price")
        if price is None:
            price = obj.get("discountedPrice")

        old_price = obj.get("originalPrice")
        if old_price is None:
            old_price = obj.get("oldPrice")
        if old_price is None:
            old_price = obj.get("strikeThroughPrice")

        prod_id = obj.get("id") or obj.get("sku") or obj.get("productId") or obj.get("itemId")
        has_product_shape = bool(prod_id or obj.get("slug") or obj.get("image") or obj.get("imageUrl"))

        if name and isinstance(name, str) and price is not None and has_product_shape:
            try:
                p_val = float(price)
                old_val = float(old_price) if old_price is not None else 0.0
                clean_name = normalize_title(name)

                if len(clean_name) >= 4 and 2 <= p_val <= 50000:
                    if old_val <= p_val or old_val > p_val * 7:
                        old_val = 0.0

                    img = obj.get("image") or obj.get("imageUrl") or obj.get("thumbnail") or ""
                    if isinstance(img, dict):
                        img = img.get("url") or img.get("src") or ""

                    slug = str(obj.get("slug") or obj.get("seoSlug") or "")
                    full_key = clean_name.lower()

                    promo_texts = [clean_name]
                    collect_promo_strings(obj, promo_texts)
                    promo_type, promo_label = classify_special_promo(promo_texts)

                    existing = results.get(full_key)
                    if not existing or old_val > existing.get("oldPrice", 0) or (promo_type and not existing.get("promoType")):
                        results[full_key] = {
                            "prodId": str(prod_id) if prod_id else full_key,
                            "title": clean_name,
                            "currPrice": p_val,
                            "oldPrice": old_val,
                            "imgUrl": str(img),
                            "slug": slug,
                            "promoType": promo_type,
                            "promoLabel": promo_label,
                        }
            except (ValueError, TypeError):
                pass

        for v in obj.values():
            if isinstance(v, (dict, list)):
                extract_products_from_next_data(v, results, meta)

    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                extract_products_from_next_data(item, results, meta)


DISCOVER_CATEGORIES_AND_PROMOS_JS = r"""
() => {
    const links = Array.from(document.querySelectorAll("a[href*='/talabat-mart/'], a[href*='/grocery/']"));
    const found = new Map();

    for (const a of links) {
        const cleanUrl = (a.href || "").split("?")[0].split("#")[0].replace(/\/$/, "");
        if (!cleanUrl || cleanUrl.includes("/product/")) continue;
        if (cleanUrl.endsWith("/talabat-mart") || /\/grocery\/\d+\/[^\/]+$/.test(cleanUrl)) continue;

        const txt = (a.innerText || "").trim().replace(/\s+/g, " ");
        if (txt.length >= 2 && txt.length <= 50 && !/^\d+$/.test(txt) && !found.has(cleanUrl)) {
            found.set(cleanUrl, txt);
        }
    }

    const promoBanners = [];
    const nodes = Array.from(document.querySelectorAll("banner, [role='banner'], section, div, span, p, a, button"));
    for (const el of nodes) {
        if (el.children.length > 3) continue;
        const txt = (el.innerText || "").trim().replace(/\s+/g, " ");
        if (txt.length >= 5 && txt.length <= 160) {
            if (/(?:1\s*\+\s*1|2\s*for\s*1|buy\s*1\s*get\s*1|bogo|spend\s*\d+|buy\s*\d+.*get|اشتر[يِ]?\s*(?:بـ|\d+).*?(?:خد|خذ|خصم|وفر))/i.test(txt)) {
                if (!promoBanners.includes(txt)) promoBanners.push(txt);
            }
        }
    }

    return {
        categories: Array.from(found.entries()).map(([url, name]) => ({ name, url })),
        promoBanners: promoBanners.slice(0, 20)
    };
}
"""


def is_ignored_category(name, url):
    s = f"{name} {url}".lower()
    return any(k.lower() in s for k in IGNORED_CATEGORIES)


def is_deals_category(name, url):
    s = f"{name} {url}".lower()
    return any(k.lower() in s for k in DEALS_PRIORITY_CATEGORIES)


def is_supermarket_category(name, url):
    s = f"{name} {url}".lower()
    return any(k.lower() in s for k in SUPERMARKET_PRIORITY_CATEGORIES)


def enqueue_url(shared_state, name, url, page_num=1):
    if len(shared_state["visited"]) >= MAX_URLS_LIMIT + shared_state["skipped_count"]:
        return

    norm_url = url.rstrip("/")
    if norm_url in shared_state["visited"]:
        return
    shared_state["visited"].add(norm_url)

    if is_ignored_category(name, norm_url):
        shared_state["skipped_count"] += 1
        return

    item = (name, norm_url, page_num)
    if is_deals_category(name, norm_url):
        shared_state["deals_queue"].append(item)
    elif is_supermarket_category(name, norm_url):
        shared_state["market_queue"].append(item)
    else:
        shared_state["other_queue"].append(item)


def pop_next_url(shared_state):
    if shared_state["deals_queue"]:
        return shared_state["deals_queue"].popleft()
    if shared_state["market_queue"]:
        return shared_state["market_queue"].popleft()
    if shared_state["other_queue"]:
        return shared_state["other_queue"].popleft()
    return None


def total_remaining(shared_state):
    return (
        len(shared_state["deals_queue"])
        + len(shared_state["market_queue"])
        + len(shared_state["other_queue"])
    )


async def scan_single_url(context, cat_name, url, page_num, shared_state):
    page = await context.new_page()
    await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=22000)
        await asyncio.sleep(0.8)

        actual_url = page.url.split("?")[0].split("#")[0].rstrip("/")
        if "/grocery/" in actual_url:
            m = re.match(r"(https://www\.talabat\.com/egypt/grocery/\d+/[^/]+)", actual_url)
            if m:
                shared_state["store_base_url"] = m.group(1)

        if shared_state["scanned_count"] <= 2:
            body_txt = await page.inner_text("body")
            if "غير متاح" in body_txt or "not available in your area" in body_txt.lower():
                maybe_send_alert(shared_state["sent_deals"], "طلبات مارت غير متاح للإحداثيات أو الفرع الحالي.")

        heading = await page.evaluate("""() => {
            const h = document.querySelector("h1, h2");
            return h ? h.innerText.trim() : "";
        }""")
        display_cat = heading if (heading and len(heading) > 2) else cat_name

        # 1. استخراج بيانات __NEXT_DATA__ الخاصة بهذه الصفحة فقط
        next_data_raw = await page.evaluate("""() => {
            const el = document.querySelector("script#__NEXT_DATA__");
            return el ? el.textContent : "";
        }""")

        merged_items = {}
        page_meta = {"total_pages": 1}
        if next_data_raw:
            try:
                parsed_json = json.loads(next_data_raw)
                extract_products_from_next_data(parsed_json, merged_items, page_meta)
            except Exception:
                pass

        # 2. اكتشاف روابط الأقسام الجانبية وبانرات العروض الخاصة في الصفحة الحالية
        discovered = await page.evaluate(DISCOVER_CATEGORIES_AND_PROMOS_JS)
        for sc in discovered.get("categories", []):
            sc_url = sc["url"]
            if "/talabat-mart/" in sc_url:
                cat_tail = sc_url.split("/talabat-mart/")[1]
                sc_url = f"{shared_state['store_base_url']}/{cat_tail}"
            enqueue_url(shared_state, sc["name"], sc_url, page_num=1)

        page_promo_type, page_promo_label = classify_special_promo(discovered.get("promoBanners", []))

        total_in_cat = len(merged_items)
        shared_state["total_products"] += total_in_cat

        # 3. تقليب ذكي بالتسلسل: يفتح الصفحة التالية فقط إذا امتلأت الصفحة الحالية بـ 20 منتج
        if total_in_cat >= 20 and page_num < MAX_PAGES_PER_CATEGORY:
            base_cat_url = actual_url if "/grocery/" in actual_url else url.split("?")[0]
            if "/grocery/" in base_cat_url:
                next_p = page_num + 1
                sep = "&" if "?" in base_cat_url else "?"
                enqueue_url(shared_state, cat_name, f"{base_cat_url}{sep}page={next_p}", page_num=next_p)

        if total_in_cat > 0:
            shared_state["sent_deals"].pop("__ALERT_TS__", None)

        price_history = shared_state["sent_deals"].setdefault("__PRICE_HISTORY__", {})
        now_ts = int(time.time())
        discounted_count = 0
        max_disc = 0
        sent_cnt = 0

        # إرسال عرض القسم العام المشروط (مثل اشتري بـ 200 وخذ 50%) مرة واحدة للقسم
        if page_promo_type and not is_ignored_category(display_cat, url):
            promo_key = f"PROMO_{display_cat.lower()}_{page_promo_label.lower()[:80]}"
            if promo_key not in shared_state["sent_in_run"] and get_saved_promo(shared_state["sent_deals"], promo_key) != page_promo_label:
                shared_state["sent_in_run"].add(promo_key)
                badge_title = "🎁 عرض 1+1 في طلبات مارت!" if page_promo_type == "bogo" else "🎯 عرض مشروط / باقة في طلبات مارت!"
                clean_page_promo = page_promo_label.replace("`", "'")
                promo_caption = (
                    f"{badge_title}\n"
                    f"🏷️ القسم: {display_cat}\n\n"
                    f"📌 العرض: `{clean_page_promo}`"
                )
                buttons = [[{"text": f"📂 فتح {display_cat[:22]}", "url": url}]]
                if send_telegram_deal(promo_caption, buttons=buttons):
                    record_deal(shared_state["sent_deals"], promo_key, 0, promo=page_promo_label)
                    sent_cnt += 1
                    shared_state["total_sent"] += 1

        for full_key, it in merged_items.items():
            curr_p = it["currPrice"]
            old_p = it.get("oldPrice", 0)
            is_price_drop = False
            promo_type = it.get("promoType")
            promo_label = it.get("promoLabel", "")

            hist_entry = price_history.get(full_key)
            if hist_entry and isinstance(hist_entry, dict):
                hist_p = hist_entry.get("p", 0)
                if old_p <= curr_p and hist_p > curr_p and hist_p <= curr_p * 4:
                    old_p = hist_p
                    is_price_drop = True
                elif curr_p > hist_p:
                    price_history[full_key] = {"p": curr_p, "ts": now_ts}
                else:
                    hist_entry["ts"] = now_ts
            else:
                base_p = old_p if old_p > curr_p else curr_p
                price_history[full_key] = {"p": base_p, "ts": now_ts}

            has_valid_discount = (old_p > curr_p and old_p <= curr_p * 7)
            disc = round(((old_p - curr_p) / old_p) * 100) if has_valid_discount else 0
            if has_valid_discount:
                discounted_count += 1
                if disc > max_disc and disc <= MAX_REALISTIC_DISCOUNT:
                    max_disc = disc

            qualifies_discount = has_valid_discount and (MIN_DISCOUNT_PERCENT <= disc <= MAX_REALISTIC_DISCOUNT)
            qualifies_special = promo_type in ("bogo", "spend_promo")

            if qualifies_discount or qualifies_special:
                title_str = it["title"]
                safe_title = title_str.replace("`", "'")
                search_url, web_url = build_best_product_url(it, url)
                unit_info = calculate_unit_price_info(title_str, curr_p)
                unit_line = f"\n{unit_info}" if unit_info else ""

                if qualifies_special:
                    sent_dedup_key = f"SPECIAL_{full_key}_{curr_p}_{promo_label[:50]}"
                    if sent_dedup_key in shared_state["sent_in_run"]:
                        continue
                    shared_state["sent_in_run"].add(sent_dedup_key)

                    unique_key = f"V92_SP_{full_key}"
                    if get_saved_promo(shared_state["sent_deals"], unique_key) == promo_label and get_saved_price(shared_state["sent_deals"], unique_key) == curr_p:
                        continue

                    if promo_type == "bogo":
                        header = "🎁 عرض 1+1 مجاناً في Talabat Mart!"
                    else:
                        header = "🎯 عرض مشروط (اشتري واكسب) في Talabat Mart!"

                    old_line = f"\n❌ السعر قبل الخصم: {old_p} EGP ({disc}%)" if has_valid_discount else ""
                    clean_item_promo = promo_label.replace("`", "'") if promo_label else ""
                    promo_line = f"\n✨ الشرط / العرض: `{clean_item_promo}`" if clean_item_promo else ""

                    caption = (
                        f"{header}\n"
                        f"🏷️ القائمة: {display_cat}\n\n"
                        f"📦 `{safe_title}`\n"
                        f"💰 السعر الظاهر: {curr_p} EGP"
                        f"{old_line}"
                        f"{promo_line}"
                        f"{unit_line}"
                    )

                    buttons = [
                        [{"text": "🔍 بحث بالاسم في طلبات", "url": search_url}],
                        [
                            {"text": "🌐 فتح في المتصفح", "url": web_url},
                            {"text": f"📂 {display_cat[:18]}", "url": url}
                        ]
                    ]

                    if send_telegram_deal(caption, img_url=it.get("imgUrl", ""), buttons=buttons):
                        record_deal(shared_state["sent_deals"], unique_key, curr_p, promo=promo_label)
                        sent_cnt += 1
                        shared_state["total_sent"] += 1
                        tag = "🎁 1+1" if promo_type == "bogo" else "🎯 مشروط"
                        print(f"   {tag}: {title_str[:40]} ({curr_p} ج)")
                    continue

                sent_dedup_key = f"{full_key}_{curr_p}"
                if sent_dedup_key in shared_state["sent_in_run"]:
                    continue
                shared_state["sent_in_run"].add(sent_dedup_key)

                unique_key = f"V9_{full_key}"
                prev_p = get_saved_price(shared_state["sent_deals"], unique_key)

                if not prev_p or prev_p > curr_p:
                    saved_egp = round(old_p - curr_p, 2)

                    if is_price_drop:
                        header = f"📉 هبوط مؤكد في السعر بنسبة {disc}%! 🔥"
                        old_label = f"❌ السعر السابق: {old_p} EGP (وفرت {saved_egp} ج)"
                    elif disc >= FLASH_DISCOUNT_PERCENT:
                        header = f"🚨 تصفية حقيقية في Talabat Mart بخصم {disc}%! 🔥"
                        old_label = f"❌ السعر القديم: {old_p} EGP (وفرت {saved_egp} ج)"
                    else:
                        header = f"🟠 لقطة حقيقية في Talabat Mart بخصم {disc}%!"
                        old_label = f"❌ السعر القديم: {old_p} EGP (وفرت {saved_egp} ج)"

                    caption = (
                        f"{header}\n"
                        f"🏷️ القائمة: {display_cat}\n\n"
                        f"📦 `{safe_title}`\n"
                        f"💰 السعر الجديد: {curr_p} EGP\n"
                        f"{old_label}"
                        f"{unit_line}"
                    )

                    buttons = [
                        [{"text": "🔍 بحث بالاسم في طلبات", "url": search_url}],
                        [
                            {"text": "🌐 فتح في المتصفح", "url": web_url},
                            {"text": f"📂 {display_cat[:18]}", "url": url}
                        ]
                    ]

                    if send_telegram_deal(caption, img_url=it.get("imgUrl", ""), buttons=buttons):
                        record_deal(shared_state["sent_deals"], unique_key, curr_p)
                        price_history[full_key] = {"p": curr_p, "ts": now_ts}
                        sent_cnt += 1
                        shared_state["total_sent"] += 1
                        tag = "📉 هبوط" if is_price_drop else ("🚨 تصفية" if disc >= FLASH_DISCOUNT_PERCENT else "✅ لقطة")
                        print(f"   {tag} [{disc}%]: {title_str[:40]} ({curr_p} ج)")

        idx = shared_state["scanned_count"]
        rem = total_remaining(shared_state)
        pg_tag = f"[ص{page_num}]" if page_num > 1 else ""
        print(f"[{idx}/{idx + rem}] {cat_name[:22]:<22} {pg_tag:<5} | منتجات: {total_in_cat:<3} | مخفض: {discounted_count:<2} | أعلى خصم: {max_disc}% | أُرسل: {sent_cnt}")

    except Exception as e:
        print(f"❌ خطأ في {cat_name}: {e}")
    finally:
        await page.close()


async def main_async():
    start_time = time.time()
    sent_deals = load_and_clean_sent()
    cookies = parse_playwright_cookies(TALABAT_COOKIES)

    print(f"⚡ بدء الرادار المتوازي V9.2.1 (خصومات {MIN_DISCOUNT_PERCENT}%+ + عروض 1+1 والعروض المشروطة)...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            locale="ar-EG",
            viewport={"width": 1366, "height": 900},
            geolocation={"latitude": LATITUDE, "longitude": LONGITUDE},
            permissions=["geolocation"],
            extra_http_headers={"Accept-Language": "ar-EG,ar;q=0.9,en-US;q=0.8"}
        )

        await context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font")
            else route.continue_(),
        )

        if cookies:
            await context.add_cookies(cookies)

        shared_state = {
            "deals_queue": deque(),
            "market_queue": deque(),
            "other_queue": deque(),
            "visited": set(),
            "sent_deals": sent_deals,
            "sent_in_run": set(),
            "store_base_url": STORE_BASE_URL,
            "scanned_count": 0,
            "skipped_count": 0,
            "total_products": 0,
            "total_sent": 0,
        }

        for label, u in SEED_URLS:
            clean_u = u.split("?")[0].split("#")[0].rstrip("/")
            enqueue_url(shared_state, label, clean_u, page_num=1)

        while total_remaining(shared_state) > 0 and shared_state["scanned_count"] < MAX_URLS_LIMIT:
            batch = []
            while total_remaining(shared_state) > 0 and len(batch) < CONCURRENT_TABS and shared_state["scanned_count"] < MAX_URLS_LIMIT:
                nxt = pop_next_url(shared_state)
                if nxt:
                    shared_state["scanned_count"] += 1
                    batch.append(nxt)

            tasks = [scan_single_url(context, name, url, p_num, shared_state) for name, url, p_num in batch]
            await asyncio.gather(*tasks)

        await browser.close()

    if shared_state["total_products"] == 0:
        maybe_send_alert(sent_deals, "لم يتم سحب أي منتجات — تأكد من صلاحية TALABAT_COOKIES أو TALABAT_MART_URL.")

    save_sent(sent_deals)
    elapsed = round(time.time() - start_time, 1)
    hist_size = len(sent_deals.get("__PRICE_HISTORY__", {}))
    print(f"\n🏁 اكتمل المسح المتوازي في {elapsed} ثانية! | صفحات: {shared_state['scanned_count']} | منتجات: {shared_state['total_products']} | سجل الأسعار: {hist_size} | مرسل: {shared_state['total_sent']}")


if __name__ == "__main__":
    asyncio.run(main_async())
