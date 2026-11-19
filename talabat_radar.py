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

SEED_URLS = [
    ("الصفحة الرئيسية لطلبات مارت", "https://www.talabat.com/egypt/talabat-mart"),
    ("عروض الموسم", "https://www.talabat.com/egypt/talabat-mart/fruit-veg/best-in-season"),
]

MIN_DISCOUNT_PERCENT = 25      # الحد الأدنى للخصم أو الهبوط المفاجئ في السعر
FLASH_DISCOUNT_PERCENT = 50    # الحد الأدنى للتنبيه الناري
MAX_REALISTIC_DISCOUNT = 92
CACHE_TTL_DAYS = 7
PRICE_HISTORY_TTL_DAYS = 21
COOKIE_ALERT_COOLDOWN = 12 * 3600
STATE_FILE = "sent_talabat.json"
MAX_CATEGORIES_LIMIT = 115
CONCURRENT_TABS = 5
MAX_PAGES_PER_CATEGORY = 6     # يقلب حتى 6 صفحات (1, 2, 3, 4, 5, 6) داخل كل قسم

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
            if v.get("ts", 0) >= cutoff and "price" in v:
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


def record_deal(sent_deals, key, price):
    sent_deals[key] = {"price": price, "ts": int(time.time())}


def save_sent(data):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ================= بناء رابط المنتج المظبوط =================
def build_best_product_url(item, category_url):
    raw_link = (item.get("productUrl") or "").strip()

    if raw_link:
        if raw_link.startswith("http") and "talabat.com" in raw_link:
            return raw_link.split("?")[0], True
        if raw_link.startswith("/"):
            return f"https://www.talabat.com{raw_link.split('?')[0]}", True

    slug = (item.get("slug") or "").strip()
    prod_id = str(item.get("prodId") or "").strip()
    if slug and not slug.startswith("http") and not slug.startswith("/"):
        if prod_id and not prod_id.startswith(("API_", "DOM_")) and prod_id not in slug:
            return f"https://www.talabat.com/egypt/talabat-mart/product/{slug}/{prod_id}", True
        return f"https://www.talabat.com/egypt/talabat-mart/product/{slug}", True

    title = item.get("title", "")
    clean_words = re.sub(r"[\+\-\(\)\[\],/\\\|×\*]+", " ", title)
    short_query = " ".join(clean_words.split()[:3])
    encoded_q = urllib.parse.quote(short_query)
    return f"https://www.talabat.com/egypt/talabat-mart/search?q={encoded_q}", False


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
                payload = {"chat_id": cid, "photo": clean_img, "caption": caption[:1020]}
                if reply_markup:
                    payload["reply_markup"] = reply_markup
                r = requests.post(photo_api, json=payload, timeout=12)
                if r.status_code == 200:
                    delivered, sent_ok = True, True
            except Exception:
                pass

        if not delivered:
            try:
                payload = {"chat_id": cid, "text": caption, "disable_web_page_preview": False}
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
            "📍 راجع صلاحية TALABAT_COOKIES لفرع الإسكندرية."
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


# ================= استخراج كل المنتجات (المخفضة والعادية) =================
def normalize_title(title):
    return re.sub(r"\s+", " ", str(title)).strip()


def walk_talabat_json(obj, api_products, stats):
    if isinstance(obj, dict):
        name = obj.get("name") or obj.get("title") or obj.get("productName")
        price = obj.get("price") or obj.get("discountedPrice") or obj.get("finalPrice")
        old_price = (
            obj.get("originalPrice")
            or obj.get("oldPrice")
            or obj.get("strikeThroughPrice")
            or obj.get("priceBeforeDiscount")
            or obj.get("basePrice")
        )
        prod_id = obj.get("id") or obj.get("sku") or obj.get("productId") or obj.get("itemId")
        img = obj.get("image") or obj.get("imageUrl") or obj.get("thumbnail") or ""
        if isinstance(img, dict):
            img = img.get("url") or img.get("src") or ""

        raw_url = obj.get("url") or obj.get("productUrl") or obj.get("href") or ""
        slug = obj.get("slug") or obj.get("seoSlug") or ""

        try:
            p_val = float(price) if price is not None else 0.0
            old_val = float(old_price) if old_price is not None else 0.0

            if name and isinstance(name, str) and len(name.strip()) > 3 and p_val >= 2:
                clean_name = normalize_title(name)
                stats["seen_titles"].add(clean_name.lower())
                key = str(prod_id) if prod_id else f"API_{clean_name[:40]}"
                existing = api_products.get(key)
                if not existing or old_val > existing.get("oldPrice", 0):
                    api_products[key] = {
                        "prodId": key,
                        "title": clean_name,
                        "currPrice": p_val,
                        "oldPrice": old_val if old_val > p_val else 0.0,
                        "imgUrl": str(img),
                        "productUrl": str(raw_url),
                        "slug": str(slug),
                    }
        except (ValueError, TypeError):
            pass

        for v in obj.values():
            if isinstance(v, (dict, list)):
                walk_talabat_json(v, api_products, stats)

    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                walk_talabat_json(item, api_products, stats)


EXTRACT_DOM_JS = r"""
() => {
    function arToEn(str) {
        if (!str) return "";
        const ar = "٠١٢٣٤٥٦٧٨٩", en = "0123456789";
        return str.replace(/[٠-٩]/g, d => en[ar.indexOf(d)]);
    }
    function cleanNum(str) {
        const s = arToEn(str || "").replace(/,/g, "").replace(/٬/g, "");
        const m = s.match(/\d+(?:\.\d+)?/g);
        return m ? m.map(Number).filter(v => v >= 2 && v <= 50000) : [];
    }
    function isStrike(el) {
        if (!el || el.nodeType !== 1) return false;
        if (["S", "DEL", "STRIKE"].includes(el.tagName)) return true;
        const cls = (typeof el.className === "string" ? el.className : "").toLowerCase();
        if (cls.includes("line-through") || cls.includes("strike") || cls.includes("old") || cls.includes("before")) return true;
        const st = window.getComputedStyle(el);
        return (st.textDecorationLine || st.textDecoration || "").includes("line-through");
    }

    const products = new Map();
    const allCardTitles = [];

    const imgs = Array.from(document.querySelectorAll("img")).filter(img => {
        const src = img.src || "";
        return (src.includes("talabat") || src.includes("deliveryhero") || src.includes("dhmedia"));
    });

    imgs.forEach(img => {
        let card = img.parentElement;
        let bestCard = null;
        for (let i = 0; i < 8 && card && card !== document.body; i++) {
            const txt = arToEn(card.innerText || "");
            const hasEgp = /(?:EGP|ج\.م|جنيه)/i.test(txt);
            const imgCount = card.querySelectorAll("img").length;
            if (hasEgp && imgCount <= 2 && txt.length >= 10 && txt.length <= 550) {
                bestCard = card;
            }
            if (imgCount > 2) break;
            card = card.parentElement;
        }

        if (!bestCard) return;

        let title = (img.alt || "").trim();
        if (title.length < 4) {
            const lines = (bestCard.innerText || "").split(/\r?\n/).map(l => l.trim()).filter(l => {
                return l.length >= 4 && !/^(?:EGP|ج\.م|جنيه|\d+(\.\d+)?$|-?\d+%|خصم|أضف|إضافة|Add|\+)$/i.test(arToEn(l));
            });
            title = lines[0] || "";
        }
        if (!title) return;
        allCardTitles.push(title);

        const oldPrices = [];
        const currPrices = [];
        const allNodes = Array.from(bestCard.querySelectorAll("*"));

        allNodes.forEach(node => {
            if (node.children.length > 0) return;
            const raw = (node.innerText || "").trim();
            if (!raw || raw.includes("%")) return;
            if (!/(?:EGP|ج\.م|جنيه|\d)/i.test(arToEn(raw))) return;

            let struck = false;
            let p = node;
            while (p && p !== bestCard) {
                if (isStrike(p)) { struck = true; break; }
                p = p.parentElement;
            }

            const nums = cleanNum(raw);
            if (struck) {
                nums.forEach(n => { if (!oldPrices.includes(n)) oldPrices.push(n); });
            } else if (/(?:EGP|ج\.م|جنيه)/i.test(arToEn(raw))) {
                nums.forEach(n => { if (!currPrices.includes(n)) currPrices.push(n); });
            }
        });

        if (currPrices.length === 0) return;

        const linkEl = bestCard.querySelector("a[href*='/product/']") || bestCard.querySelector("a[href]") || bestCard.closest("a[href]");
        const href = linkEl ? linkEl.href : "";

        const key = ("DOM_" + title.slice(0, 40));
        if (!products.has(key)) {
            products.set(key, {
                prodId: key,
                title: title.replace(/\s+/g, " ").trim(),
                currPrice: Math.min(...currPrices),
                oldPrice: oldPrices.length > 0 ? Math.max(...oldPrices) : 0,
                imgUrl: img.src || "",
                productUrl: href,
                slug: ""
            });
        }
    });

    return { allCardTitles, items: Array.from(products.values()) };
}
"""

# العثور على شريط الترقيم (← 1 2 3 4 →) في أسفل الصفحة بدقة متناهية وإعطاء إحداثيات زرار الصفحة المطلوبة
FIND_PAGINATION_BUTTON_JS = r"""
(targetPageNum) => {
    const targetStr = String(targetPageNum);
    // جمع كل المربعات الصغيرة التي تحتوي على أرقام صفحات (1, 2, 3...)
    const boxes = [];
    const allEls = Array.from(document.querySelectorAll("button, a, li, div, span"));
    for (const el of allEls) {
        if (el.children.length > 0) continue;
        const txt = (el.innerText || "").trim();
        if (!/^[1-9]\d?$/.test(txt)) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width >= 15 && rect.width <= 65 && rect.height >= 15 && rect.height <= 65 && rect.left > 220) {
            boxes.push({ el, txt, rect });
        }
    }

    // البحث عن عنصر برقم الصفحة المطلوبة بحيث يوجد بجانبه أفقياً رقم صفحة آخر (مثل "1" و "2" في نفس السطر)
    for (const b of boxes) {
        if (b.txt !== targetStr) continue;
        const hasSiblingInSameRow = boxes.some(other =>
            other !== b &&
            other.txt !== b.txt &&
            Math.abs(other.rect.top - b.rect.top) < 12 &&
            Math.abs(other.rect.left - b.rect.left) < 350
        );
        if (hasSiblingInSameRow) {
            b.el.scrollIntoView({ block: "center" });
            const r = b.el.getBoundingClientRect();
            return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
        }
    }
    return null;
}
"""

DISCOVER_SIDEBAR_JS = r"""
() => {
    const links = Array.from(document.querySelectorAll("a[href*='/talabat-mart/']"));
    const found = new Map();
    for (const a of links) {
        const cleanUrl = (a.href || "").split("?")[0].split("#")[0].replace(/\/$/, "");
        if (!cleanUrl || cleanUrl.includes("/product/") || cleanUrl.endsWith("/talabat-mart")) continue;

        const afterMart = cleanUrl.split("/talabat-mart/")[1] || "";
        const segments = afterMart.split("/").filter(Boolean);
        if (segments.length < 2) continue;

        const txt = (a.innerText || "").trim().replace(/\s+/g, " ");
        if (txt.length >= 2 && txt.length <= 50 && !found.has(cleanUrl)) {
            found.set(cleanUrl, txt);
        }
    }
    return Array.from(found.entries()).map(([url, name]) => ({ name, url }));
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


def enqueue_category(shared_state, name, url):
    if is_deals_category(name, url):
        shared_state["deals_queue"].append((name, url))
    elif is_supermarket_category(name, url):
        shared_state["market_queue"].append((name, url))
    else:
        shared_state["other_queue"].append((name, url))


def pop_next_category(shared_state):
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


async def scan_single_category(context, cat_name, url, shared_state):
    page = await context.new_page()
    await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    api_products = {}
    page_stats = {"seen_titles": set()}

    async def on_response(resp):
        try:
            ct = resp.headers.get("content-type", "")
            if resp.status == 200 and ("json" in ct or "api" in resp.url):
                data = await resp.json()
                walk_talabat_json(data, api_products, page_stats)
        except Exception:
            pass

    page.on("response", on_response)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=22000)
        await asyncio.sleep(0.9)

        if shared_state["scanned_count"] <= 2:
            body_txt = await page.inner_text("body")
            if "غير متاح" in body_txt or "not available in your area" in body_txt.lower():
                maybe_send_alert(shared_state["sent_deals"], "طلبات مارت غير متاح للإحداثيات الحالية.")

        heading = await page.evaluate("""() => {
            const h = document.querySelector("h1, h2");
            return h ? h.innerText.trim() : "";
        }""")
        display_cat = heading if (heading and len(heading) > 2) else cat_name

        try:
            next_scripts = await page.evaluate(
                """() => Array.from(document.querySelectorAll("script#__NEXT_DATA__, script[type='application/json']")).map(s => s.textContent)"""
            )
            for raw_j in next_scripts:
                if raw_j:
                    try:
                        walk_talabat_json(json.loads(raw_j), api_products, page_stats)
                    except Exception:
                        pass
        except Exception:
            pass

        dom_items = {}
        pages_visited_in_cat = 1
        prev_page_signature = None

        for page_num in range(1, MAX_PAGES_PER_CATEGORY + 1):
            # 1. النزول لأسفل الصفحة أولاً ليظهر شريط الترقيم (← 1 2 3 4 →)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.35)

            if page_num > 1:
                # 2. البحث عن إحداثيات مربع رقم الصفحة التالية (2, 3, 4...) وضغطه بالماوس الحقيقي
                coords = await page.evaluate(FIND_PAGINATION_BUTTON_JS, page_num)
                if not coords:
                    break
                await page.mouse.click(coords["x"], coords["y"])
                await asyncio.sleep(1.2)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(0.3)

            if page_num == 1:
                sidebar_cats = await page.evaluate(DISCOVER_SIDEBAR_JS)
                for sc in sidebar_cats:
                    sc_url = sc["url"]
                    if sc_url not in shared_state["visited"]:
                        shared_state["visited"].add(sc_url)
                        if is_ignored_category(sc["name"], sc_url):
                            shared_state["skipped_count"] += 1
                        elif len(shared_state["visited"]) < MAX_CATEGORIES_LIMIT + shared_state["skipped_count"]:
                            enqueue_category(shared_state, sc["name"], sc_url)

            res = await page.evaluate(EXTRACT_DOM_JS)
            current_titles = res.get("allCardTitles", [])
            current_sig = tuple(sorted(current_titles[:5]))

            if page_num > 1 and (not current_titles or current_sig == prev_page_signature):
                break

            prev_page_signature = current_sig
            pages_visited_in_cat = page_num

            for t in current_titles:
                page_stats["seen_titles"].add(normalize_title(t).lower())
            for it in res.get("items", []):
                dom_items[it["prodId"]] = it

        merged_items = {}
        for v in dom_items.values():
            merged_items[v["title"][:40].lower()] = v
        for v in api_products.values():
            k = v["title"][:40].lower()
            existing = merged_items.get(k)
            if existing:
                if not v.get("productUrl") and existing.get("productUrl"):
                    v["productUrl"] = existing["productUrl"]
                if existing.get("oldPrice", 0) > v.get("oldPrice", 0):
                    v["oldPrice"] = existing["oldPrice"]
            merged_items[k] = v

        total_in_cat = max(len(page_stats["seen_titles"]), len(merged_items))
        shared_state["total_products"] += total_in_cat

        if total_in_cat > 0:
            shared_state["sent_deals"].pop("__ALERT_TS__", None)

        price_history = shared_state["sent_deals"].setdefault("__PRICE_HISTORY__", {})
        now_ts = int(time.time())
        discounted_count = 0
        max_disc = 0
        sent_cnt = 0

        for norm_key, it in merged_items.items():
            if norm_key in shared_state["seen_titles"]:
                continue
            shared_state["seen_titles"].add(norm_key)

            curr_p = it["currPrice"]
            old_p = it.get("oldPrice", 0)
            is_price_drop = False

            hist_entry = price_history.get(norm_key)
            if hist_entry and isinstance(hist_entry, dict):
                hist_p = hist_entry.get("p", 0)
                if old_p <= curr_p and hist_p > curr_p:
                    old_p = hist_p
                    is_price_drop = True
                elif curr_p > hist_p:
                    price_history[norm_key] = {"p": curr_p, "ts": now_ts}
                else:
                    hist_entry["ts"] = now_ts
            else:
                base_p = old_p if old_p > curr_p else curr_p
                price_history[norm_key] = {"p": base_p, "ts": now_ts}

            if old_p <= curr_p or old_p > curr_p * 12:
                continue

            discounted_count += 1
            disc = round(((old_p - curr_p) / old_p) * 100)
            if disc > max_disc and disc <= MAX_REALISTIC_DISCOUNT:
                max_disc = disc

            if MIN_DISCOUNT_PERCENT <= disc <= MAX_REALISTIC_DISCOUNT:
                title_str = it["title"]
                unique_key = it["prodId"]
                title_cache_key = f"TITLE_{norm_key[:32]}"

                prev_p = get_saved_price(shared_state["sent_deals"], unique_key)
                prev_title_p = get_saved_price(shared_state["sent_deals"], title_cache_key)

                if (not prev_p or prev_p > curr_p) and (prev_title_p != curr_p):
                    best_url, is_direct = build_best_product_url(it, url)
                    btn_label = "🛍️ فتح المنتج مباشرة" if is_direct else "🔍 ابحث عن المنتج في طلبات"

                    saved_egp = round(old_p - curr_p, 2)
                    unit_info = calculate_unit_price_info(title_str, curr_p)
                    unit_line = f"\n{unit_info}" if unit_info else ""

                    if is_price_drop:
                        header = f"📉 هبوط مفاجئ في السعر بنسبة {disc}%! 🔥"
                        old_label = f"❌ السعر السابق المسجل: {old_p} EGP (وفرت {saved_egp} ج)"
                    elif disc >= FLASH_DISCOUNT_PERCENT:
                        header = f"🚨 تصفية نارية في Talabat Mart بخصم {disc}%! 🔥"
                        old_label = f"❌ السعر القديم: {old_p} EGP (وفرت {saved_egp} ج)"
                    else:
                        header = f"🟠 لقطة Talabat Mart بخصم {disc}%!"
                        old_label = f"❌ السعر القديم: {old_p} EGP (وفرت {saved_egp} ج)"

                    caption = (
                        f"{header}\n"
                        f"🏷️ القائمة: {display_cat}\n\n"
                        f"📦 {title_str}\n"
                        f"💰 السعر الجديد: {curr_p} EGP\n"
                        f"{old_label}"
                        f"{unit_line}"
                    )

                    buttons = [
                        [{"text": btn_label, "url": best_url}],
                        [{"text": f"📂 فتح قائمة {display_cat[:24]}", "url": url}]
                    ]

                    if send_telegram_deal(caption, img_url=it.get("imgUrl", ""), buttons=buttons):
                        record_deal(shared_state["sent_deals"], unique_key, curr_p)
                        record_deal(shared_state["sent_deals"], title_cache_key, curr_p)
                        price_history[norm_key] = {"p": curr_p, "ts": now_ts}
                        sent_cnt += 1
                        shared_state["total_sent"] += 1
                        tag = "📉 هبوط" if is_price_drop else ("🚨 تصفية" if disc >= FLASH_DISCOUNT_PERCENT else "✅ لقطة")
                        print(f"   {tag} [{disc}%]: {title_str[:40]} ({curr_p} ج)")

        idx = shared_state["scanned_count"]
        rem = total_remaining(shared_state)
        pg_tag = f"({pages_visited_in_cat}ص)" if pages_visited_in_cat > 1 else ""
        print(f"[{idx}/{idx + rem}] {cat_name[:23]:<23} {pg_tag:<4} | منتجات: {total_in_cat:<3} | مخفض: {discounted_count:<2} | أعلى خصم: {max_disc}% | أُرسل: {sent_cnt}")

    except Exception as e:
        print(f"❌ خطأ في {cat_name}: {e}")
    finally:
        await page.close()


async def main_async():
    start_time = time.time()
    sent_deals = load_and_clean_sent()
    cookies = parse_playwright_cookies(TALABAT_COOKIES)

    print(f"⚡ بدء الرادار المتوازي V7.3 (تقليب الصفحات 1..{MAX_PAGES_PER_CATEGORY} | خصم {MIN_DISCOUNT_PERCENT}%+)...")

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
            "seen_titles": set(),
            "scanned_count": 0,
            "skipped_count": 0,
            "total_products": 0,
            "total_sent": 0,
        }

        for label, u in SEED_URLS:
            clean_u = u.split("?")[0].split("#")[0].rstrip("/")
            shared_state["visited"].add(clean_u)
            enqueue_category(shared_state, label, clean_u)

        while total_remaining(shared_state) > 0 and shared_state["scanned_count"] < MAX_CATEGORIES_LIMIT:
            batch = []
            while total_remaining(shared_state) > 0 and len(batch) < CONCURRENT_TABS and shared_state["scanned_count"] < MAX_CATEGORIES_LIMIT:
                nxt = pop_next_category(shared_state)
                if nxt:
                    shared_state["scanned_count"] += 1
                    batch.append(nxt)

            tasks = [scan_single_category(context, name, url, shared_state) for name, url in batch]
            await asyncio.gather(*tasks)

        await browser.close()

    if shared_state["total_products"] == 0:
        maybe_send_alert(sent_deals, "لم يتم سحب أي منتجات — تأكد من صلاحية TALABAT_COOKIES.")

    save_sent(sent_deals)
    elapsed = round(time.time() - start_time, 1)
    hist_size = len(sent_deals.get("__PRICE_HISTORY__", {}))
    print(f"\n🏁 اكتمل المسح المتوازي في {elapsed} ثانية! | قوائم: {shared_state['scanned_count']} | منتجات: {shared_state['total_products']} | سجل الأسعار: {hist_size} | مرسل: {shared_state['total_sent']}")


if __name__ == "__main__":
    asyncio.run(main_async())
