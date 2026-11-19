import os
import re
import json
import time
import urllib.parse
import requests
from playwright.sync_api import sync_playwright

# ================= الإعدادات والمتغيرات البيئية =================
TELEGRAM_TOKEN = (os.environ.get("TALABAT_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_TOKEN") or "").strip()
CHAT_ID = (os.environ.get("TALABAT_CHAT_ID") or os.environ.get("CHAT_ID") or "").strip()
TALABAT_COOKIES = os.environ.get("TALABAT_COOKIES", "").strip()

# إحداثيات الموقع لتحديد فرع طلبات مارت الأقرب (مثال افتراضي: القاهرة الجديدة / المعادي - عدلها لإحداثياتك)
LATITUDE = float(os.environ.get("TALABAT_LAT") or "29.9602")
LONGITUDE = float(os.environ.get("TALABAT_LNG") or "31.2569")


# رابط متجر طلبات مارت الرئيسي في مصر (يمكنك وضع رابط فرع منطقتك مباشرة إذا أردت)
TALABAT_MART_URL = os.environ.get(
    "TALABAT_MART_URL",
    "https://www.talabat.com/egypt/grocery/623082/talabat-mart"
).strip()

MIN_DISCOUNT_PERCENT = 30
CACHE_TTL_DAYS = 7
COOKIE_ALERT_COOLDOWN = 12 * 3600
STATE_FILE = "sent_talabat.json"


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


# ================= إرسال تنبيهات تليجرام =================
def send_telegram_text(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    sent_ok = False
    for cid in [c.strip() for c in CHAT_ID.split(",") if c.strip()]:
        try:
            r = requests.post(url, json={"chat_id": cid, "text": text}, timeout=15)
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

    # تحسين جودة صورة طلبات إن أمكن
    clean_img = img_url.split("?")[0] if img_url else ""

    sent_ok = False
    for cid in [c.strip() for c in CHAT_ID.split(",") if c.strip()]:
        delivered = False
        if clean_img and clean_img.startswith("http"):
            try:
                payload = {"chat_id": cid, "photo": clean_img, "caption": caption[:1020]}
                if reply_markup:
                    payload["reply_markup"] = reply_markup
                r = requests.post(photo_api, json=payload, timeout=15)
                if r.status_code == 200:
                    delivered = True
                    sent_ok = True
            except Exception:
                pass

        if not delivered:
            try:
                payload = {"chat_id": cid, "text": caption, "disable_web_page_preview": False}
                if reply_markup:
                    payload["reply_markup"] = reply_markup
                r = requests.post(msg_api, json=payload, timeout=15)
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
            "📍 تأكد من صلاحية TALABAT_COOKIES أو إحداثيات العنوان (TALABAT_LAT / TALABAT_LNG) وأن فرع طلبات مارت متاح في المنطقة."
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
        print("⚠️ صيغة TALABAT_COOKIES غير صحيحة! تأكد من نسخها بصيغة Export as JSON.")
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


# ================= التقاط البيانات من API طلبات (Network Sniffer) =================
def walk_talabat_json(obj, api_products):
    """يستخرج المنتجات المخفضة مباشرة من استجابات API الخاصة بطلبات مارت"""
    if isinstance(obj, dict):
        # البحث عن أنماط كائنات المنتجات في API طلبات
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
            p_val = float(price) if price is not None else 0
            old_val = float(old_price) if old_price is not None else 0
            if name and isinstance(name, str) and len(name.strip()) > 3 and p_val >= 2 and old_val > p_val:
                key = str(prod_id) if prod_id else f"API_{name.strip()[:35]}"
                api_products[key] = {
                    "prodId": key,
                    "title": name.strip(),
                    "currPrices": [p_val],
                    "oldPrices": [old_val],
                    "imgUrl": str(img),
                    "href": obj.get("url") or obj.get("deepLink") or ""
                }
        except (ValueError, TypeError):
            pass

        for v in obj.values():
            if isinstance(v, (dict, list)):
                walk_talabat_json(v, api_products)

    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                walk_talabat_json(item, api_products)


# ================= استخراج المنتجات من الواجهة (DOM) =================
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

    const results = new Map();
    const imgs = Array.from(document.querySelectorAll("img")).filter(img => {
        const src = img.src || "";
        return (src.includes("talabat") || src.includes("deliveryhero") || src.includes("dhmedia")) && img.width >= 40;
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

        const oldPrices = [];
        const currPrices = [];
        const allNodes = Array.from(bestCard.querySelectorAll("*"));

        allNodes.forEach(node => {
            if (node.children.length > 0) return;
            const raw = (node.innerText || "").trim();
            if (!raw || raw.includes("%")) return;
            if (!/(?:EGP|ج\.م|جنيه|\d)/i.test(arToEn(raw))) return;

            // فحص ما إذا كان العنصر أو أحد آبائه مشطوباً
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

        const linkEl = bestCard.querySelector("a[href]") || bestCard.closest("a[href]");
        const href = linkEl ? linkEl.href : "";
        const key = ("DOM_" + title.slice(0, 35));

        if (!results.has(key)) {
            results.set(key, {
                prodId: key,
                title,
                currPrices,
                oldPrices,
                imgUrl: img.src || "",
                href
            });
        }
    });

    return Array.from(results.values());
}
"""

DISCOVER_CATEGORIES_JS = r"""
() => {
    const links = Array.from(document.querySelectorAll("a[href]"));
    const cats = new Map();
    for (const a of links) {
        const href = a.href || "";
        const txt = (a.innerText || "").trim().replace(/\s+/g, " ");
        // روابط أقسام طلبات مارت عادة تحتوي على category أو aisle أو نفس مسار المتجر
        if (
            (href.includes("/grocery/") || href.includes("/talabat-mart")) &&
            (href.includes("category") || href.includes("aisle") || href.includes("collection") || href.split("/").length >= 6) &&
            txt.length >= 2 && txt.length <= 45 &&
            !/^(?:الرئيسية|Home|تسجيل|Login|السلة|Cart)$/i.test(txt)
        ) {
            if (!cats.has(href)) {
                cats.set(href, txt);
            }
        }
    }
    return Array.from(cats.entries()).map(([url, name]) => ({ name, url }));
}
"""

SCROLL_JS = """
() => {
    window.scrollBy(0, 850);
    document.querySelectorAll("div, main, section").forEach(el => {
        if (el.scrollHeight > el.clientHeight + 250) {
            const ov = window.getComputedStyle(el).overflowY;
            if (ov === "auto" || ov === "scroll") el.scrollBy(0, 850);
        }
    });
}
"""


def main():
    sent_deals = load_and_clean_sent()
    cookies = parse_playwright_cookies(TALABAT_COOKIES)

    print(f"⚡ بدء فحص Talabat Mart الشامل (خصم فعلي {MIN_DISCOUNT_PERCENT}%+)...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
            locale="ar-EG",
            viewport={"width": 412, "height": 915},
            geolocation={"latitude": LATITUDE, "longitude": LONGITUDE},
            permissions=["geolocation"],
            extra_http_headers={"Accept-Language": "ar-EG,ar;q=0.9,en-US;q=0.8"}
        )

        if cookies:
            context.add_cookies(cookies)

        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        api_products = {}

        def on_response(resp):
            try:
                ct = resp.headers.get("content-type", "")
                if resp.status == 200 and ("json" in ct or "api" in resp.url):
                    data = resp.json()
                    walk_talabat_json(data, api_products)
            except Exception:
                pass

        page.on("response", on_response)

        # 1. فتح الصفحة الرئيسية لطلبات مارت لاكتشاف جميع الأقسام
        print(f"🔍 فتح متجر طلبات مارت واكتشاف الأقسام...")
        try:
            page.goto(TALABAT_MART_URL, wait_until="domcontentloaded", timeout=40000)
            time.sleep(5)
        except Exception as e:
            print(f"❌ تعذر فتح صفحة طلبات مارت: {e}")
            maybe_send_alert(sent_deals, f"تعذر الوصول لصفحة طلبات مارت: {e}")
            browser.close()
            return

        body_txt = page.inner_text("body")
        if "غير متاح" in body_txt or "not available in your area" in body_txt.lower() or "select your area" in body_txt.lower():
            print("⚠️ طلبات مارت غير متاح للإحداثيات الحالية أو يطلب تحديد العنوان.")
            maybe_send_alert(sent_deals, "المتجر يطلب تحديد المنطقة أو غير متاح حالياً في الإحداثيات المضبوطة.")

        # التمرير قليلاً في الصفحة الرئيسية لتحميل شريط الأقسام
        for _ in range(4):
            page.evaluate(SCROLL_JS)
            time.sleep(1.2)

        discovered = page.evaluate(DISCOVER_CATEGORIES_JS)
        targets = [("الصفحة الرئيسية والعروض", TALABAT_MART_URL)]
        seen_urls = {TALABAT_MART_URL}

        for cat in discovered:
            u = cat["url"]
            if u not in seen_urls:
                seen_urls.add(u)
                targets.append((cat["name"], u))

        print(f"📂 تم اكتشاف {len(targets)} قسم/صفحة داخل طلبات مارت للفحص.")

        # 2. المرور على كل الأقسام واستخراج العروض
        for idx, (cat_name, url) in enumerate(targets, 1):
            print(f"\n[{idx}/{len(targets)}] فحص قسم: {cat_name}...")
            api_products.clear()

            try:
                if idx > 1:
                    page.goto(url, wait_until="domcontentloaded", timeout=35000)
                    time.sleep(3.5)

                # استخراج __NEXT_DATA__ إن وجد
                try:
                    next_scripts = page.evaluate(
                        """() => Array.from(document.querySelectorAll("script#__NEXT_DATA__, script[type='application/json']")).map(s => s.textContent)"""
                    )
                    for raw_j in next_scripts:
                        if raw_j:
                            try:
                                walk_talabat_json(json.loads(raw_j), api_products)
                            except Exception:
                                pass
                except Exception:
                    pass

                last_total = 0
                stagnant = 0
                dom_items = {}

                for _ in range(16):
                    extracted = page.evaluate(EXTRACT_DOM_JS)
                    for it in extracted:
                        dom_items[it["prodId"]] = it

                    total_now = len(api_products) + len(dom_items)
                    if total_now == last_total:
                        stagnant += 1
                    else:
                        stagnant = 0
                    last_total = total_now

                    if stagnant >= 4 and total_now > 0:
                        break

                    page.evaluate(SCROLL_JS)
                    time.sleep(1.2)

                # دمج نتائج API (الأدق) مع نتائج DOM
                merged_items = {}
                for k, v in dom_items.items():
                    merged_items[v["title"][:35]] = v
                for k, v in api_products.items():
                    merged_items[v["title"][:35]] = v  # أولوية لبيانات API

                if len(merged_items) > 0:
                    sent_deals.pop("__ALERT_TS__", None)

                max_disc = 0
                sent_cnt = 0

                for title_key_short, it in merged_items.items():
                    curr_prices = it.get("currPrices") or []
                    old_prices = it.get("oldPrices") or []
                    if not curr_prices or not old_prices:
                        continue

                    curr_p = min(curr_prices)
                    old_p = max(old_prices)

                    if old_p <= curr_p or old_p > curr_p * 10:
                        continue

                    disc = round(((old_p - curr_p) / old_p) * 100)
                    if disc > max_disc:
                        max_disc = disc

                    if disc >= MIN_DISCOUNT_PERCENT:
                        title_str = it.get("title", "منتج عرض Talabat Mart")
                        unique_key = it.get("prodId") or f"TB_{title_str[:35]}"
                        title_cache_key = f"TITLE_{title_str[:30]}"

                        prev_p = get_saved_price(sent_deals, unique_key)
                        prev_title_p = get_saved_price(sent_deals, title_cache_key)

                        if (not prev_p or prev_p > curr_p) and (prev_title_p != curr_p):
                            raw_href = it.get("href", "")
                            if raw_href and raw_href.startswith("/"):
                                final_link = f"https://www.talabat.com{raw_href}"
                            elif raw_href.startswith("http"):
                                final_link = raw_href
                            else:
                                final_link = url

                            saved_egp = round(old_p - curr_p, 2)
                            caption = (
                                f"🟠 لقطة Talabat Mart بخصم فعلي {disc}%!\n"
                                f"🏷️ القسم: {cat_name}\n\n"
                                f"📦 {title_str}\n"
                                f"💰 السعر الجديد: {curr_p} EGP\n"
                                f"❌ السعر القديم: {old_p} EGP (وفرت {saved_egp} ج)"
                            )

                            buttons = [
                                [{"text": "🛒 اطلب من طلبات مارت", "url": final_link}],
                                [{"text": f"📂 قسم {cat_name}", "url": url}]
                            ]

                            if send_telegram_deal(caption, img_url=it.get("imgUrl", ""), buttons=buttons):
                                record_deal(sent_deals, unique_key, curr_p)
                                record_deal(sent_deals, title_cache_key, curr_p)
                                sent_cnt += 1
                                print(f"   ✅ تم إرسال عرض: {title_str[:40]} (خصم {disc}%)")
                                time.sleep(0.4)

                print(f"   📊 فُحص {len(merged_items)} منتج مخفض | أعلى خصم: {max_disc}% | أُرسل: {sent_cnt}")

            except Exception as e:
                print(f"   ❌ خطأ أثناء فحص {cat_name}: {e}")

        browser.close()

    save_sent(sent_deals)
    print("\n🏁 اكتمل فحص طلبات مارت!")


if __name__ == "__main__":
    main()
