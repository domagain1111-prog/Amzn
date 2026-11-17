import os
import json
import time
import urllib.parse
import requests
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
AMAZON_COOKIES = os.environ.get("AMAZON_COOKIES", "").strip()

MIN_DISCOUNT_PERCENT = 30
STATE_FILE = "sent_now.json"

NOW_TARGETS = [
    ("أقوى العروض (Top Deals)", "https://www.amazon.eg/tez/browse/explore/category?qcbrand=sAuWWBROaG&nodeId=220630950031&title=Top+Deals"),
    ("الشوكولاتة والحلويات", "https://www.amazon.eg/tez/browse/explore/category?qcbrand=sAuWWBROaG&nodeId=215093316031&title=Chocolate+and+Candies"),
    ("السناكس والحلويات", "https://www.amazon.eg/tez/browse/explore/category?qcbrand=sAuWWBROaG&nodeId=215093298031&title=Snack+%26+Sweets"),
    ("القهوة والشاي", "https://www.amazon.eg/tez/browse/explore/category?qcbrand=sAuWWBROaG&nodeId=212925569031&title=Coffee+and+Tea"),
    ("خزين المطبخ والبقالة", "https://www.amazon.eg/tez/browse/explore/category?qcbrand=sAuWWBROaG&nodeId=212925568031&title=Food+Cupboard"),
    ("المخبوزات", "https://www.amazon.eg/tez/browse/explore/category?qcbrand=sAuWWBROaG&nodeId=215052334031&title=Bakery"),
    ("الخضروات والفاكهة", "https://www.amazon.eg/tez/browse/explore/category?qcbrand=sAuWWBROaG&nodeId=214841697031&title=Fruits+and+Vegetables"),
]

def load_sent():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}

def save_sent(data):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    sent_ok = False
    for cid in [c.strip() for c in CHAT_ID.split(",") if c.strip()]:
        try:
            r = requests.post(url, json={"chat_id": cid, "text": text}, timeout=15)
            if r.status_code == 200:
                sent_ok = True
        except Exception as e:
            print(f"   ❌ خطأ إرسال تيليجرام لـ {cid}: {e}")
    return sent_ok

def parse_playwright_cookies(raw_json):
    if not raw_json:
        return []
    try:
        raw_list = json.loads(raw_json)
    except Exception:
        print("⚠️ صيغة AMAZON_COOKIES غير صحيحة! تأكد من نسخها بصيغة Export as JSON.")
        return []

    cleaned = []
    for c in raw_list:
        if not isinstance(c, dict) or not c.get("name") or c.get("value") is None:
            continue
        item = {
            "name": str(c["name"]),
            "value": str(c["value"]),
            "domain": c.get("domain", ".amazon.eg"),
            "path": c.get("path", "/"),
            "secure": bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", False)),
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

# كود اعتراض طلبات الشبكة الخلفية لربط صور المنتجات وأسمائها بكود ASIN الحقيقي
EXTRACT_JS = """
(apiAsinMap) => {
    function arToEn(str) {
        if (!str) return "";
        const ar = "٠١٢٣٤٥٦٧٨٩", en = "0123456789";
        return str.replace(/[٠-٩]/g, d => en[ar.indexOf(d)]);
    }
    function cleanNum(str) {
        if (!str) return null;
        let s = arToEn(str).replace(/,/g, "").replace(/٬/g, "");
        let m = s.match(/\\d+(?:\\.\\d+)?/);
        return m ? parseFloat(m[0]) : null;
    }

    // فحص بيانات Next.js المدمجة في الصفحة لربط كود الصورة بالـ ASIN
    const localMap = Object.assign({}, apiAsinMap || {});
    document.querySelectorAll("script#__NEXT_DATA__, script[type='application/json']").forEach(sc => {
        const txt = sc.textContent || "";
        // ربط أي ASIN بصورة أمازون القريبة منه في الـ JSON
        const regex = /"asin"\\s*:\\s*"([B0-9][A-Z0-9]{9})"[\\s\\S]{0,600}?\\/images\\/I\\/([A-Za-z0-9+%-]+)\\.|\\/images\\/I\\/([A-Za-z0-9+%-]+)\\.[\\s\\S]{0,600}?"asin"\\s*:\\s*"([B0-9][A-Z0-9]{9})"/gi;
        let m;
        while ((m = regex.exec(txt)) !== null) {
            const asin = m[1] || m[4];
            const imgId = m[2] || m[3];
            if (asin && imgId) localMap[imgId] = asin;
        }
    });

    // استخراج الـ ASIN من خصائص React الداخلية للعنصر إن وجدت
    function findReactAsin(domEl) {
        let cur = domEl;
        for (let depth = 0; depth < 6 && cur; depth++) {
            const keys = Object.keys(cur);
            const reactKey = keys.find(k => k.startsWith("__reactProps$") || k.startsWith("__reactFiber$"));
            if (reactKey) {
                try {
                    const str = JSON.stringify(cur[reactKey], (k, v) => (k.startsWith("_") || typeof v === "function" || v instanceof Node) ? undefined : v);
                    if (str) {
                        const m = str.match(/"(?:asin|productId|id)"\\s*:\\s*"([B0-9][A-Z0-9]{9})"/i) || str.match(/\\b(B0[A-Z0-9]{8})\\b/);
                        if (m) return m[1];
                    }
                } catch (e) {}
            }
            cur = cur.parentElement;
        }
        return null;
    }

    const cardsMap = new Map();
    const imgs = Array.from(document.querySelectorAll("img[src*='media-amazon.com/images/I/']"));

    imgs.forEach(img => {
        if (img.width > 0 && (img.width < 45 || img.width > 500)) return;
        let el = img.parentElement;
        let bestCard = null;

        for (let i = 0; i < 8 && el && el !== document.body; i++) {
            const txt = arToEn(el.innerText || "");
            const hasCurrency = /(?:EGP|ج\\.م|جنيه)/i.test(txt);
            const imgCount = el.querySelectorAll("img[src*='media-amazon.com/images/I/']").length;
            if (hasCurrency && imgCount === 1 && txt.length >= 12 && txt.length <= 650) {
                bestCard = el;
            }
            if (imgCount > 1) break;
            el = el.parentElement;
        }

        if (bestCard) {
            const imgKey = ((img.src.match(/\\/I\\/([A-Za-z0-9+%-]+?)\\._/) || img.src.match(/\\/I\\/([A-Za-z0-9+%-]+)\\./)) || [])[1] || "";
            const baseImgId = imgKey.split(".")[0];

            // البحث عن ASIN بكل الطرق الممكنة (الروابط، خصائص العنصر، شجرة React، أو خريطة الـ API)
            const linkEl = bestCard.querySelector("a[href*='/dp/'], a[href*='/gp/'], a[href*='/tez/product'], a[href]") || bestCard.closest("a[href]");
            const rawHref = linkEl ? linkEl.href : "";

            const htmlSearch = rawHref + " " + bestCard.outerHTML;
            const asinMatch = htmlSearch.match(/(?:\\/dp\\/|\\/gp\\/product\\/|\\/product\\/|data-asin[=:"']+|asin[=:"']+)([B0-9][A-Z0-9]{9})/i)
                           || htmlSearch.match(/\\b(B0[A-Z0-9]{8})\\b/);

            let asin = asinMatch ? asinMatch[1] : (localMap[baseImgId] || localMap[imgKey] || findReactAsin(bestCard));

            const prodId = asin ? asin : ("TEZ_" + (baseImgId || img.alt || "")).slice(0, 35);

            if (prodId && !cardsMap.has(prodId)) {
                const normText = arToEn((bestCard.innerText || "").replace(/\\s+/g, " "));
                let prices = [];
                const reg = /(?:EGP|ج\\.م\\.?|جنيه)\\s*(\\d+(?:\\.\\d+)?)|(\\d+(?:\\.\\d+)?)\\s*(?:EGP|ج\\.م\\.?|جنيه)/gi;
                let m;
                while ((m = reg.exec(normText)) !== null) {
                    let v = parseFloat(m[1] || m[2]);
                    if (v && v >= 2 && !prices.includes(v)) prices.push(v);
                }

                bestCard.querySelectorAll("s, del, strike, [style*='line-through'], [class*='strike'], [class*='line-through'], .a-text-price").forEach(sEl => {
                    let v = cleanNum(sEl.innerText);
                    if (v && v >= 2 && !prices.includes(v)) prices.push(v);
                });

                let badgeDisc = 0;
                const discRegex = /(?:-\s*|خصم\\s*|وفر\\s*)(\\d{2})\\s*%|(\\d{2})\\s*%\\s*(?:off|خصم|تخفيض)?/gi;
                let dm;
                while ((dm = discRegex.exec(normText)) !== null) {
                    let d = parseInt(dm[1] || dm[2], 10);
                    if (d > badgeDisc && d <= 95) badgeDisc = d;
                }

                let title = (img.alt && img.alt.trim().length > 6) ? img.alt.trim() : "";
                if (!title) {
                    const lines = (bestCard.innerText || "").split("\\n").map(l => l.trim()).filter(l => {
                        if (l.length < 6) return false;
                        return !/^(?:EGP|ج\\.م|جنيه|\\d+(\\.\\d+)?$|-?\\d+%|خصم|إضافة|أضف|Add)/i.test(arToEn(l));
                    });
                    title = lines[0] || "منتج عرض Amazon Now";
                }

                let productUrl = "";
                if (asin) {
                    productUrl = "https://www.amazon.eg/dp/" + asin;
                } else if (rawHref && !rawHref.includes("/explore/category")) {
                    productUrl = rawHref;
                }

                cardsMap.set(prodId, { prodId, asin: asin || "", productUrl, title, prices, badgeDisc });
            }
        }
    });

    return Array.from(cardsMap.values());
}
"""

SCROLL_JS = """
() => {
    window.scrollBy(0, 950);
    const candidates = Array.from(document.querySelectorAll("main, [role='main'], div"));
    for (const el of candidates) {
        if (el.scrollHeight > el.clientHeight + 300) {
            const st = window.getComputedStyle(el);
            if (st.overflowY === "auto" || st.overflowY === "scroll") {
                el.scrollBy(0, 950);
            }
        }
    }
}
"""

def extract_asins_from_json_text(txt, store_map):
    import re
    # يلتقط أي ASIN مقترن برابط صورته في استجابات API الخلفية أثناء التمرير
    pattern = re.compile(
        r'"asin"\s*:\s*"([B0-9][A-Z0-9]{9})"[\s\S]{0,600}?/images/I/([A-Za-z0-9+%-]+?)(?:\._|\.)|'
        r'/images/I/([A-Za-z0-9+%-]+?)(?:\._|\.)[\s\S]{0,600}?"asin"\s*:\s*"([B0-9][A-Z0-9]{9})"',
        re.IGNORECASE
    )
    for m in pattern.finditer(txt):
        asin = m.group(1) or m.group(4)
        img_id = (m.group(2) or m.group(3) or "").split(".")[0]
        if asin and img_id:
            store_map[img_id] = asin

def main():
    cookies = parse_playwright_cookies(AMAZON_COOKIES)
    if not cookies:
        print("⚠️ لم يتم العثور على AMAZON_COOKIES في Secrets، تخطي فحص Amazon Now.")
        return

    sent_deals = load_sent()
    print(f"⚡ بدء فحص Amazon Now السحابي ({len(NOW_TARGETS)} أقسام | خصم {MIN_DISCOUNT_PERCENT}%+)...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
            locale="ar-EG",
            viewport={"width": 412, "height": 915},
            extra_http_headers={"Accept-Language": "ar-EG,ar;q=0.9,en-US;q=0.8"}
        )
        context.add_cookies(cookies)
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        api_asin_map = {}

        def handle_response(response):
            try:
                ctype = response.headers.get("content-type", "")
                if ("json" in ctype or "tez" in response.url) and response.status == 200:
                    txt = response.text()
                    if "images/I/" in txt and ("asin" in txt.lower() or "B0" in txt):
                        extract_asins_from_json_text(txt, api_asin_map)
            except Exception:
                pass

        page.on("response", handle_response)

        for idx, (cat_name, url) in enumerate(NOW_TARGETS, 1):
            print(f"\n[{idx}/{len(NOW_TARGETS)}] فحص قسم: {cat_name}...")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=35000)
                time.sleep(3)

                body_txt = page.inner_text("body")
                if "لقد فقدت الاتصال" in body_txt:
                    print("   ⚠️ ظهرت رسالة 'لقد فقدت الاتصال' - قد تحتاج لتحديث AMAZON_COOKIES لاحقاً.")
                    break

                last_count = 0
                stagnant = 0
                all_items = {}

                for _ in range(18):
                    items = page.evaluate(EXTRACT_JS, api_asin_map)
                    for it in items:
                        all_items[it["prodId"]] = it

                    if len(all_items) == last_count:
                        stagnant += 1
                    else:
                        stagnant = 0
                    last_count = len(all_items)

                    if stagnant >= 3 and len(all_items) > 0:
                        break

                    page.evaluate(SCROLL_JS)
                    time.sleep(1.1)

                max_disc = 0
                sent_now_cnt = 0

                for prod_id, it in all_items.items():
                    prices = it["prices"]
                    badge_disc = it["badgeDisc"]
                    if not prices and not badge_disc:
                        continue

                    curr_p = min(prices) if prices else None
                    old_p = max(prices) if len(prices) >= 2 else None
                    disc = 0

                    if curr_p and old_p and old_p > curr_p and old_p < curr_p * 8:
                        disc = round(((old_p - curr_p) / old_p) * 100)
                    if badge_disc > disc:
                        disc = badge_disc
                        if curr_p and not old_p and disc < 95:
                            old_p = round(curr_p / (1 - disc / 100))

                    if disc > max_disc:
                        max_disc = disc

                    if disc >= MIN_DISCOUNT_PERCENT and curr_p:
                        prev_p = sent_deals.get(prod_id)
                        # فحص عدم التكرار أيضاً بالاسم والسعر في حال تغير المعرف من TEZ_ إلى ASIN
                        title_key = "TITLE_" + it["title"][:30]
                        if (not prev_p or prev_p > curr_p) and (sent_deals.get(title_key) != curr_p):
                            if it.get("productUrl"):
                                final_link = it["productUrl"]
                            else:
                                q = urllib.parse.quote_plus(it["title"])
                                final_link = f"https://www.amazon.eg/s?k={q}"

                            msg = (
                                f"⚡ لقطة Amazon Now بخصم {disc}%!\n"
                                f"🏷️ القسم: {cat_name}\n\n"
                                f"📦 {it['title']}\n"
                                f"💰 السعر الحالي: {curr_p} EGP\n"
                                f"❌ السعر قبل الخصم: {old_p if old_p else 'مخفض'} EGP\n\n"
                                f"🔗 {final_link}"
                            )
                            if send_telegram(msg):
                                sent_deals[prod_id] = curr_p
                                sent_deals[title_key] = curr_p
                                sent_now_cnt += 1
                                print(f"   ✅ تم إرسال عرض {prod_id} بخصم {disc}% -> {final_link}")
                                time.sleep(0.4)

                print(f"   📊 فُحص {len(all_items)} منتج | أعلى خصم: {max_disc}% | أُرسل: {sent_now_cnt}")

            except Exception as e:
                print(f"   ❌ خطأ أثناء فحص {cat_name}: {e}")

        browser.close()

    save_sent(sent_deals)

if __name__ == "__main__":
    main()
