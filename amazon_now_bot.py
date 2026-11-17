import os
import json
import time
import requests
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
AMAZON_COOKIES = os.environ.get("AMAZON_COOKIES", "").strip()

MIN_DISCOUNT_PERCENT = 50
STATE_FILE = "sent_now.json"

NOW_URLS = [
    "https://www.amazon.eg/tez/browse/explore/category?qcbrand=sAuWWBROaG&nodeId=220630950031&title=Top+Deals&ref=EG_Tez_SF_Top_deals"
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

EXTRACT_JS = """
() => {
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
            const linkEl = bestCard.querySelector("a[href]") || bestCard.closest("a[href]");
            const href = linkEl ? linkEl.href : location.href;
            const asinMatch = (href + " " + bestCard.innerHTML).match(/(?:\\/dp\\/|\\/gp\\/product\\/|asin[=:"']+)([A-Z0-9]{10})/i);
            const imgKey = (img.src.match(/\\/I\\/([A-Za-z0-9+%-]+)\\./) || [])[1] || img.alt || "";
            const prodId = asinMatch ? asinMatch[1] : ("TEZ_" + imgKey).slice(0, 35);

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

                cardsMap.set(prodId, { prodId, href, title, prices, badgeDisc });
            }
        }
    });

    return Array.from(cardsMap.values());
}
"""

SCROLL_JS = """
() => {
    window.scrollBy(0, 850);
    const candidates = Array.from(document.querySelectorAll("main, [role='main'], div"));
    for (const el of candidates) {
        if (el.scrollHeight > el.clientHeight + 300) {
            const st = window.getComputedStyle(el);
            if (st.overflowY === "auto" || st.overflowY === "scroll") {
                el.scrollBy(0, 850);
            }
        }
    }
}
"""

def main():
    cookies = parse_playwright_cookies(AMAZON_COOKIES)
    if not cookies:
        print("⚠️ لم يتم العثور على AMAZON_COOKIES في Secrets، تخطي فحص Amazon Now.")
        return

    sent_deals = load_sent()
    print(f"⚡ بدء فحص Amazon Now السحابي (تم تحميل {len(cookies)} كوكي جلسة)...")

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

        for idx, url in enumerate(NOW_URLS, 1):
            print(f"\n[{idx}/{len(NOW_URLS)}] فتح قسم Amazon Now...")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=35000)
                time.sleep(4)

                body_txt = page.inner_text("body")
                if "لقد فقدت الاتصال" in body_txt:
                    print("   ⚠️ ظهرت رسالة 'لقد فقدت الاتصال' - قد تحتاج لتحديث AMAZON_COOKIES لاحقاً.")
                    continue

                last_count = 0
                stagnant = 0
                all_items = {}

                for _ in range(22):
                    items = page.evaluate(EXTRACT_JS)
                    for it in items:
                        all_items[it["prodId"]] = it

                    if len(all_items) == last_count:
                        stagnant += 1
                    else:
                        stagnant = 0
                    last_count = len(all_items)

                    if stagnant >= 4 and len(all_items) > 0:
                        break

                    page.evaluate(SCROLL_JS)
                    time.sleep(1.5)

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
                        if not prev_p or prev_p > curr_p:
                            clean_link = (
                                f"https://www.amazon.eg/dp/{prod_id}"
                                if (not prod_id.startswith("TEZ_") and len(prod_id) == 10)
                                else it["href"]
                            )
                            msg = (
                                f"⚡ لقطة سحابية Amazon Now بخصم {disc}%!\n\n"
                                f"📦 {it['title']}\n"
                                f"💰 السعر الحالي: {curr_p} EGP\n"
                                f"❌ السعر قبل الخصم: {old_p if old_p else 'مخفض'} EGP\n\n"
                                f"🔗 {clean_link}"
                            )
                            if send_telegram(msg):
                                sent_deals[prod_id] = curr_p
                                sent_now_cnt += 1
                                print(f"   ✅ تم إرسال عرض {prod_id} بخصم {disc}%")

                print(f"   📊 فُحص {len(all_items)} منتج في Amazon Now | أعلى خصم: {max_disc}% | أُرسل: {sent_now_cnt}")

            except Exception as e:
                print(f"   ❌ خطأ أثناء فحص الرابط: {e}")

        browser.close()

    save_sent(sent_deals)

if __name__ == "__main__":
    main()
