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

env_url = os.environ.get("TALABAT_MART_URL", "").strip()
START_URL = (
    env_url
    if env_url.startswith("http") and "/talabat-mart" in env_url
    else "https://www.talabat.com/egypt/talabat-mart/fruit-veg/best-in-season"
)

MIN_DISCOUNT_PERCENT = 40
FLASH_DISCOUNT_PERCENT = 65
MAX_REALISTIC_DISCOUNT = 92
CACHE_TTL_DAYS = 7
COOKIE_ALERT_COOLDOWN = 12 * 3600
STATE_FILE = "sent_talabat.json"
MAX_CATEGORIES_LIMIT = 130
CONCURRENT_TABS = 5  # عدد التبويبات المفتوحة بالتوازي في نفس اللحظة

IGNORED_CATEGORIES = [
    "pet", "dog", "cat", "حيوانات", "قطط", "كلاب",
    "baby", "diaper", "infant", "أطفال", "حفاضات", "رضع",
    "stationery", "office", "مكتبية", "أدوات مدرسية",
    "tobacco", "cigarette", "vape", "سجائر", "تبغ",
    "party", "gift", "هدايا", "حفلات"
]


# ================= إدارة الكاش ومنع التكرار =================
def load_and_clean_sent():
    now = int(time.time())
    cutoff = now - (CACHE_TTL_DAYS * 86400)
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
    for k, v in data.items():
        if k == "__ALERT_TS__":
            if isinstance(v, (int, float)) and (now - v) < 86400:
                cleaned[k] = v
            continue
        if isinstance(v, dict):
            if v.get("ts", 0) >= cutoff and "price" in v:
                cleaned[k] = v
        elif isinstance(v, (int, float)):
            cleaned[k] = {"price": v, "ts": now}
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


# ================= استخراج المنتجات من JSON والواجهة =================
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

        try:
            p_val = float(price) if price is not None else 0.0
            old_val = float(old_price) if old_price is not None else 0.0

            if name and isinstance(name, str) and len(name.strip()) > 3 and p_val >= 2:
                stats["total_seen"] += 1
                if old_val > p_val:
                    clean_name = normalize_title(name)
                    key = str(prod_id) if prod_id else f"API_{clean_name[:40]}"
                    api_products[key] = {
                        "prodId": key,
                        "title": clean_name,
                        "currPrice": p_val,
                        "oldPrice": old_val,
                        "imgUrl": str(img),
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

    const discounted = new Map();
    let totalCards = 0;

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
        totalCards++;

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

        if (currPrices.length === 0 || oldPrices.length === 0) return;

        let title = (img.alt || "").trim();
        if (title.length < 4) {
            const lines = (bestCard.innerText || "").split(/\r?\n/).map(l => l.trim()).filter(l => {
                return l.length >= 4 && !/^(?:EGP|ج\.م|جنيه|\d+(\.\d+)?$|-?\d+%|خصم|أضف|إضافة|Add|\+)$/i.test(arToEn(l));
            });
            title = lines[0] || "";
        }
        if (!title) return;

        const key = ("DOM_" + title.slice(0, 40));
        if (!discounted.has(key)) {
            discounted.set(key, {
                prodId: key,
                title: title.replace(/\s+/g, " ").trim(),
                currPrice: Math.min(...currPrices),
                oldPrice: Math.max(...oldPrices),
                imgUrl: img.src || "",
            });
        }
    });

    return { totalCards, items: Array.from(discounted.values()) };
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

SCROLL_FAST_JS = """
() => {
    window.scrollBy(0, 1800);
    document.querySelectorAll("div, aside, nav, main, section, ul").forEach(el => {
        if (el.scrollHeight > el.clientHeight + 150) {
            const ov = window.getComputedStyle(el).overflowY;
            if (ov === "auto" || ov === "scroll") {
                el.scrollBy(0, 1800);
            }
        }
    });
}
"""


def is_ignored_category(name, url):
    s = f"{name} {url}".lower()
    return any(k.lower() in s for k in IGNORED_CATEGORIES)


def is_priority_category(name, url):
    s = f"{name} {url}".lower()
    return any(k in s for k in ["offer", "deal", "promo", "save", "discount", "عروض", "تخفيض", "خصم", "best-in-season"])


async def scan_single_category(context, cat_name, url, shared_state):
    page = await context.new_page()
    await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    api_products = {}
    page_stats = {"total_seen": 0}

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

        if shared_state["scanned_count"] == 1:
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
        max_cards_seen = 0
        for _ in range(3):
            sidebar_cats = await page.evaluate(DISCOVER_SIDEBAR_JS)
            for sc in sidebar_cats:
                sc_url = sc["url"]
                if sc_url not in shared_state["visited"]:
                    shared_state["visited"].add(sc_url)
                    if is_ignored_category(sc["name"], sc_url):
                        shared_state["skipped_count"] += 1
                    elif len(shared_state["visited"]) < MAX_CATEGORIES_LIMIT + shared_state["skipped_count"]:
                        if is_priority_category(sc["name"], sc_url):
                            shared_state["queue"].appendleft((sc["name"], sc_url))
                        else:
                            shared_state["queue"].append((sc["name"], sc_url))

            res = await page.evaluate(EXTRACT_DOM_JS)
            cards_now = res.get("totalCards", 0)
            if cards_now > max_cards_seen:
                max_cards_seen = cards_now
            for it in res.get("items", []):
                dom_items[it["prodId"]] = it

            await page.evaluate(SCROLL_FAST_JS)
            await asyncio.sleep(0.35)

        merged_items = {}
        for v in dom_items.values():
            merged_items[v["title"][:40].lower()] = v
        for v in api_products.values():
            merged_items[v["title"][:40].lower()] = v

        total_in_cat = max(page_stats["total_seen"], max_cards_seen, len(merged_items))
        shared_state["total_products"] += total_in_cat

        if total_in_cat > 0:
            shared_state["sent_deals"].pop("__ALERT_TS__", None)

        max_disc = 0
        sent_cnt = 0

        for norm_key, it in merged_items.items():
            if norm_key in shared_state["seen_titles"]:
                continue
            shared_state["seen_titles"].add(norm_key)

            curr_p = it["currPrice"]
            old_p = it["oldPrice"]
            if old_p <= curr_p or old_p > curr_p * 12:
                continue

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
                    clean_query = urllib.parse.quote_plus(title_str[:45])
                    search_link = f"https://www.talabat.com/egypt/talabat-mart?q={clean_query}"

                    saved_egp = round(old_p - curr_p, 2)
                    unit_info = calculate_unit_price_info(title_str, curr_p)
                    unit_line = f"\n{unit_info}" if unit_info else ""

                    header = (
                        f"🚨 تصفية نارية في Talabat Mart بخصم {disc}%! 🔥"
                        if disc >= FLASH_DISCOUNT_PERCENT
                        else f"🟠 لقطة Talabat Mart بخصم {disc}%!"
                    )

                    caption = (
                        f"{header}\n"
                        f"🏷️ القائمة: {display_cat}\n\n"
                        f"📦 {title_str}\n"
                        f"💰 السعر الجديد: {curr_p} EGP\n"
                        f"❌ السعر القديم: {old_p} EGP (وفرت {saved_egp} ج)"
                        f"{unit_line}"
                    )

                    buttons = [
                        [{"text": "🛒 اطلب المنتج من طلبات مارت", "url": search_link}],
                        [{"text": f"📂 تصفح قائمة {display_cat[:24]}", "url": url}]
                    ]

                    if send_telegram_deal(caption, img_url=it.get("imgUrl", ""), buttons=buttons):
                        record_deal(shared_state["sent_deals"], unique_key, curr_p)
                        record_deal(shared_state["sent_deals"], title_cache_key, curr_p)
                        sent_cnt += 1
                        shared_state["total_sent"] += 1
                        tag = "🚨 تصفية" if disc >= FLASH_DISCOUNT_PERCENT else "✅ لقطة"
                        print(f"   {tag} [{disc}%]: {title_str[:40]} ({curr_p} ج)")

        idx = shared_state["scanned_count"]
        rem = len(shared_state["queue"])
        print(f"[{idx}/{idx + rem}] {cat_name[:26]:<26} | منتجات: {total_in_cat:<3} | مخفض: {len(merged_items):<2} | أعلى خصم: {max_disc}% | أُرسل: {sent_cnt}")

    except Exception as e:
        print(f"❌ خطأ في {cat_name}: {e}")
    finally:
        await page.close()


async def main_async():
    start_time = time.time()
    sent_deals = load_and_clean_sent()
    cookies = parse_playwright_cookies(TALABAT_COOKIES)

    print(f"⚡ بدء الرادار المتوازي V6 ({CONCURRENT_TABS} تبويبات معاً | خصم {MIN_DISCOUNT_PERCENT}%+ | تصفية {FLASH_DISCOUNT_PERCENT}%+)...")

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
            if route.request.resource_type in ("image", "media", "font", "stylesheet")
            else route.continue_(),
        )

        if cookies:
            await context.add_cookies(cookies)

        clean_start = START_URL.split("?")[0].split("#")[0].rstrip("/")
        shared_state = {
            "queue": deque([("القسم الافتتاحي", clean_start)]),
            "visited": {clean_start},
            "sent_deals": sent_deals,
            "seen_titles": set(),
            "scanned_count": 0,
            "skipped_count": 0,
            "total_products": 0,
            "total_sent": 0,
        }

        while shared_state["queue"] and shared_state["scanned_count"] < MAX_CATEGORIES_LIMIT:
            batch = []
            while shared_state["queue"] and len(batch) < CONCURRENT_TABS and shared_state["scanned_count"] < MAX_CATEGORIES_LIMIT:
                shared_state["scanned_count"] += 1
                batch.append(shared_state["queue"].popleft())

            tasks = [scan_single_category(context, name, url, shared_state) for name, url in batch]
            await asyncio.gather(*tasks)

        await browser.close()

    if shared_state["total_products"] == 0:
        maybe_send_alert(sent_deals, "لم يتم سحب أي منتجات — تأكد من صلاحية TALABAT_COOKIES.")

    save_sent(sent_deals)
    elapsed = round(time.time() - start_time, 1)
    print(f"\n🏁 اكتمل المسح المتوازي في {elapsed} ثانية! | قوائم: {shared_state['scanned_count']} (تخطي {shared_state['skipped_count']}) | منتجات: {shared_state['total_products']} | مرسل: {shared_state['total_sent']}")


if __name__ == "__main__":
    asyncio.run(main_async())
