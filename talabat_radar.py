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

MIN_DISCOUNT_PERCENT = 25      # الحد الأدنى للخصم الفعلي المشطوب
FLASH_DISCOUNT_PERCENT = 50    # الحد الأدنى للتنبيه الناري
MAX_REALISTIC_DISCOUNT = 85    # استبعاد أي خصم غير منطقي فوق 85%
CACHE_TTL_DAYS = 7
PRICE_HISTORY_TTL_DAYS = 21
COOKIE_ALERT_COOLDOWN = 12 * 3600
STATE_FILE = "sent_talabat.json"
MAX_URLS_LIMIT = 240
CONCURRENT_TABS = 5
MAX_PAGES_PER_CATEGORY = 6

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
            # تجاهل المفاتيح القديمة المقصوصة (أقل من 42 حرف وكانت مقصوصة) وتحديثها بالاسم الكامل
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


# ================= بناء روابط البحث والمتصفح =================
def build_best_product_url(item, category_url):
    title = item.get("title", "")
    clean_words = re.sub(r"[\+\-\(\)\[\],/\\\|×\*]+", " ", title)
    short_query = " ".join(clean_words.split()[:4])
    encoded_q = urllib.parse.quote(short_query)
    search_url = f"https://www.talabat.com/egypt/talabat-mart/search?q={encoded_q}"

    raw_link = (item.get("productUrl") or "").strip()
    if raw_link.startswith("http") and "talabat.com" in raw_link:
        web_url = raw_link.split("?")[0]
    elif raw_link.startswith("/"):
        web_url = f"https://www.talabat.com{raw_link.split('?')[0]}"
    else:
        web_url = search_url

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


# ================= استخراج المنتجات من الواجهة الفعلية فقط بدقة عالية =================
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
        if (cls.includes("line-through") || cls.includes("strike")) return true;
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
            if (hasEgp && imgCount <= 2 && txt.length >= 10 && txt.length <= 500) {
                bestCard = card;
            }
            if (imgCount > 2) break;
            card = card.parentElement;
        }

        if (!bestCard) return;

        // استخراج الاسم الكامل غير المقصوص من النص داخل البطاقة أولاً، ثم من alt الصورة
        const lines = (bestCard.innerText || "").split(/\r?\n/).map(l => l.trim()).filter(l => {
            return l.length >= 4 && !/^(?:EGP|ج\.م|جنيه|Save\s*\d+%|\d+(\.\d+)?$|-?\d+%|خصم|أضف|إضافة|Add|\+|x\d+)$/i.test(arToEn(l));
        });
        let title = (img.alt || "").trim();
        if (lines.length > 0 && lines[0].length > title.length && !lines[0].endsWith("...")) {
            title = lines[0];
        }
        if (!title || title.length < 4) return;
        allCardTitles.push(title);

        // قراءة شارة الخصم المكتوبة على البطاقة (مثل Save 25%) للتحقق الإضافي
        const cardTextEn = arToEn(bestCard.innerText || "");
        const badgeMatch = cardTextEn.match(/(?:Save|خصم)\s*(\d+)\s*%/i);
        const badgePct = badgeMatch ? parseInt(badgeMatch[1], 10) : 0;

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

        const currPrice = Math.min(...currPrices);
        let oldPrice = oldPrices.length > 0 ? Math.max(...oldPrices) : 0;

        // تحقق صارم: السعر القديم يجب أن يكون أكبر من الحالي ولا يتجاوز 7 أضعاف السعر الحالي
        if (oldPrice <= currPrice || oldPrice > currPrice * 7) {
            oldPrice = 0;
        }

        // إذا وُجدت شارة Save X% على البطاقة، تأكد أن النسبة المحسوبة قريبة منها (±5%) لمنع أي قراءة خاطئة
        if (oldPrice > 0 && badgePct > 0) {
            const calcPct = Math.round(((oldPrice - currPrice) / oldPrice) * 100);
            if (Math.abs(calcPct - badgePct) > 6) {
                oldPrice = 0;
            }
        }

        const linkEl = bestCard.querySelector("a[href*='/product/']") || bestCard.querySelector("a[href]") || bestCard.closest("a[href]");
        const href = linkEl ? linkEl.href : "";

        const fullKey = title.replace(/\s+/g, " ").trim().toLowerCase();
        if (!products.has(fullKey)) {
            products.set(fullKey, {
                prodId: fullKey,
                title: title.replace(/\s+/g, " ").trim(),
                currPrice: currPrice,
                oldPrice: oldPrice,
                imgUrl: img.src || "",
                productUrl: href,
                slug: ""
            });
        }
    });

    return { allCardTitles, items: Array.from(products.values()) };
}
"""

DISCOVER_LINKS_AND_PAGES_JS = r"""
() => {
    const links = Array.from(document.querySelectorAll("a[href*='/talabat-mart/'], a[href*='/grocery/']"));
    const found = new Map();
    let maxPageDetected = 1;

    for (const a of links) {
        const fullHref = a.href || "";
        const pageMatch = fullHref.match(/[?&]page=(\d+)/);
        if (pageMatch) {
            const pNum = parseInt(pageMatch[1], 10);
            if (pNum > maxPageDetected && pNum <= 15) maxPageDetected = pNum;
        }

        const cleanUrl = fullHref.split("?")[0].split("#")[0].replace(/\/$/, "");
        if (!cleanUrl || cleanUrl.includes("/product/")) continue;
        if (cleanUrl.endsWith("/talabat-mart") || /\/grocery\/\d+\/[^\/]+$/.test(cleanUrl)) continue;

        const txt = (a.innerText || "").trim().replace(/\s+/g, " ");
        if (txt.length >= 2 && txt.length <= 50 && !/^\d+$/.test(txt) && !found.has(cleanUrl)) {
            found.set(cleanUrl, txt);
        }
    }

    const boxes = [];
    document.querySelectorAll("button, a, li, div, span").forEach(el => {
        if (el.children.length > 0) return;
        const txt = (el.innerText || "").trim();
        if (!/^[1-9]\d?$/.test(txt)) return;
        const r = el.getBoundingClientRect();
        if (r.width >= 14 && r.width <= 70 && r.height >= 14 && r.height <= 70 && r.left > 220) {
            boxes.push({ num: parseInt(txt, 10), top: r.top, left: r.left });
        }
    });

    for (const b of boxes) {
        const hasSibling = boxes.some(o => o !== b && o.num !== b.num && Math.abs(o.top - b.top) < 15 && Math.abs(o.left - b.left) < 400);
        if (hasSibling && b.num > maxPageDetected && b.num <= 15) {
            maxPageDetected = b.num;
        }
    }

    return {
        categories: Array.from(found.entries()).map(([url, name]) => ({ name, url })),
        maxPageDetected
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
        await asyncio.sleep(1.1)

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

        await page.evaluate("""() => {
            window.scrollTo(0, document.body.scrollHeight);
            document.querySelectorAll("div, main, section").forEach(el => {
                if (el.scrollHeight > el.clientHeight + 100 && el.clientWidth > 400) {
                    el.scrollTop = el.scrollHeight;
                }
            });
        }""")
        await asyncio.sleep(0.35)

        discovery = await page.evaluate(DISCOVER_LINKS_AND_PAGES_JS)
        for sc in discovery.get("categories", []):
            sc_url = sc["url"]
            if "/talabat-mart/" in sc_url:
                cat_tail = sc_url.split("/talabat-mart/")[1]
                sc_url = f"{shared_state['store_base_url']}/{cat_tail}"
            enqueue_url(shared_state, sc["name"], sc_url, page_num=1)

        res = await page.evaluate(EXTRACT_DOM_JS)
        merged_items = {it["prodId"]: it for it in res.get("items", [])}
        total_in_cat = len(merged_items)
        shared_state["total_products"] += total_in_cat

        if page_num == 1 and total_in_cat >= 20:
            base_cat_url = actual_url if "/grocery/" in actual_url else url.split("?")[0]
            if "/grocery/" in base_cat_url:
                detected_pages = max(discovery.get("maxPageDetected", 1), 3)
                target_max_p = min(detected_pages, MAX_PAGES_PER_CATEGORY)
                for next_p in range(2, target_max_p + 1):
                    sep = "&" if "?" in base_cat_url else "?"
                    enqueue_url(shared_state, cat_name, f"{base_cat_url}{sep}page={next_p}", page_num=next_p)

        if total_in_cat > 0:
            shared_state["sent_deals"].pop("__ALERT_TS__", None)

        price_history = shared_state["sent_deals"].setdefault("__PRICE_HISTORY__", {})
        now_ts = int(time.time())
        discounted_count = 0
        max_disc = 0
        sent_cnt = 0

        for full_key, it in merged_items.items():
            # تجاهل الأسماء المقصوصة بـ "..." في تتبع تغير الأسعار حتى لا تتداخل مع أحجام مختلفة
            is_truncated = full_key.endswith("...")
            curr_p = it["currPrice"]
            old_p = it.get("oldPrice", 0)
            is_price_drop = False

            if not is_truncated:
                hist_entry = price_history.get(full_key)
                if hist_entry and isinstance(hist_entry, dict):
                    hist_p = hist_entry.get("p", 0)
                    # لا يُعتبر هبوطاً حقيقياً إلا إذا كان السعر السابق مسجلاً من نفس الفرع ومؤكداً
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

            if old_p <= curr_p or old_p > curr_p * 7:
                continue

            discounted_count += 1
            disc = round(((old_p - curr_p) / old_p) * 100)
            if disc > max_disc and disc <= MAX_REALISTIC_DISCOUNT:
                max_disc = disc

            if MIN_DISCOUNT_PERCENT <= disc <= MAX_REALISTIC_DISCOUNT:
                sent_dedup_key = f"{full_key}_{curr_p}"
                if sent_dedup_key in shared_state["sent_in_run"]:
                    continue
                shared_state["sent_in_run"].add(sent_dedup_key)

                title_str = it["title"]
                unique_key = f"FULL_{full_key}"

                prev_p = get_saved_price(shared_state["sent_deals"], unique_key)

                if not prev_p or prev_p > curr_p:
                    search_url, web_url = build_best_product_url(it, url)

                    saved_egp = round(old_p - curr_p, 2)
                    unit_info = calculate_unit_price_info(title_str, curr_p)
                    unit_line = f"\n{unit_info}" if unit_info else ""
                    safe_title = title_str.replace("`", "'")

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
                        if not is_truncated:
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

    print(f"⚡ بدء الرادار المتوازي V8.2 (دقة أسعار 100% + نسخ الاسم بلمسة واحدة | خصم {MIN_DISCOUNT_PERCENT}%+)...")

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
