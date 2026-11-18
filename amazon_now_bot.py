import os
import re
import json
import time
import urllib.parse
import requests
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = (os.environ.get("NOW_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_TOKEN") or "").strip()
CHAT_ID = (os.environ.get("NOW_CHAT_ID") or os.environ.get("CHAT_ID") or "").strip()
AMAZON_COOKIES = os.environ.get("AMAZON_COOKIES", "").strip()

# نسبة الخصم الفعلية المحسوبة من فرق السعرين (40% - ولو تقصد ربع السعر الأصلي اجعلها 75)
MIN_DISCOUNT_PERCENT = 40
STATE_FILE = "sent_now.json"

NOW_TARGETS = [
    ("أقوى العروض (Top Deals)", "https://www.amazon.eg/tez/browse/explore/category?qcbrand=sAuWWBROaG&nodeId=220630950031&title=Top+Deals&ref=EG_Tez_SF_Top_deals"),
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
        print("   ⚠️ توكن تيليجرام أو الـ CHAT_ID غير موجود!")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    sent_ok = False
    for cid in [c.strip() for c in CHAT_ID.split(",") if c.strip()]:
        try:
            r = requests.post(url, json={"chat_id": cid, "text": text}, timeout=15)
            if r.status_code == 200:
                sent_ok = True
            else:
                print(f"   ⚠️ رد تيليجرام لـ {cid}: {r.status_code} - {r.text}")
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

# استخراج السعر القديم (المشطوب فعلياً) والسعر الحالي فقط وتجاهل نسب أمازون المكتوبة
EXTRACT_JS = r"""
() => {
    function arToEn(str) {
        if (!str) return "";
        const ar = "٠١٢٣٤٥٦٧٨٩", en = "0123456789";
        return str.replace(/[٠-٩]/g, d => en[ar.indexOf(d)]);
    }
    function cleanPriceText(str) {
        if (!str) return "";
        return arToEn(str)
            .replace(/,/g, "")
            .replace(/٬/g, "")
            .replace(/(\d)\s+\.(\d)/g, "$1.$2");
    }
    function extractNumbers(str) {
        const s = cleanPriceText(str);
        const matches = s.match(/\d+(?:\.\d+)?/g);
        return matches ? matches.map(Number).filter(v => v >= 2) : [];
    }
    function isStrikeNode(node) {
        if (!node || node.nodeType !== 1) return false;
        if (["S", "DEL", "STRIKE"].includes(node.tagName)) return true;
        const cls = (typeof node.className === "string" ? node.className : "").toLowerCase();
        if (cls.includes("line-through") || cls.includes("a-text-price") || cls.includes("strike") || node.getAttribute("data-a-strike") === "true") {
            return true;
        }
        const style = window.getComputedStyle(node);
        const dec = (style.textDecorationLine || style.textDecoration || "").toLowerCase();
        return dec.includes("line-through");
    }

    const cardsMap = new Map();
    const imgs = Array.from(document.querySelectorAll("img[src*='media-amazon.com/images/I/']"));

    imgs.forEach(img => {
        if (img.width > 0 && (img.width < 45 || img.width > 500)) return;
        let el = img.parentElement;
        let bestCard = null;

        for (let i = 0; i < 8 && el && el !== document.body; i++) {
            const txt = arToEn(el.innerText || "");
            const hasCurrency = /(?:EGP|ج\.م|جنيه)/i.test(txt);
            const imgCount = el.querySelectorAll("img[src*='media-amazon.com/images/I/']").length;
            if (hasCurrency && imgCount === 1 && txt.length >= 12 && txt.length <= 650) {
                bestCard = el;
            }
            if (imgCount > 1) break;
            el = el.parentElement;
        }

        if (bestCard) {
            const linkEl = bestCard.querySelector("a[href]") || bestCard.closest("a[href]");
            const href = linkEl ? linkEl.href : "";
            const asinMatch = (href + " " + bestCard.innerHTML).match(/(?:\/dp\/|\/gp\/product\/|asin[=:"']+)([B0-9][A-Z0-9]{9})/i);
            const imgFile = (img.src.split("/I/")[1] || "").split("?")[0];
            const imgId = imgFile.split(".")[0];
            const prodId = asinMatch ? asinMatch[1] : ("TEZ_" + (imgId || img.alt || "")).slice(0, 35);

            if (prodId && !cardsMap.has(prodId)) {
                const origNodes = Array.from(bestCard.querySelectorAll("*"));
                const clone = bestCard.cloneNode(true);
                const cloneNodes = Array.from(clone.querySelectorAll("*"));

                let oldPrices = [];

                // 1. سحب السعر القديم المشطوب فقط وحذفه من النسخة حتى لا يختلط بالسعر الجديد
                origNodes.forEach((node, idx) => {
                    if (isStrikeNode(node)) {
                        let hasStrikeParent = false;
                        let p = node.parentElement;
                        while (p && p !== bestCard) {
                            if (isStrikeNode(p)) {
                                hasStrikeParent = true;
                                break;
                            }
                            p = p.parentElement;
                        }
                        if (!hasStrikeParent) {
                            const offscreen = node.querySelector(".a-offscreen");
                            const rawStrike = offscreen ? offscreen.textContent : (node.innerText || "");
                            if (!rawStrike.includes("%")) {
                                extractNumbers(rawStrike).forEach(v => {
                                    if (!oldPrices.includes(v)) oldPrices.push(v);
                                });
                            }
                        }
                        if (cloneNodes[idx]) {
                            cloneNodes[idx].remove();
                        }
                    }
                });

                // 2. سحب السعر الحالي مع استبعاد سعر الجرام/الكيلو/الوحدة وأي نسب مئوية
                let remText = cleanPriceText((clone.innerText || "").replace(/\s+/g, " "));
                remText = remText.replace(/\([^)]*(?:\/|لكل)[^)]*\)/gi, " ");
                remText = remText.replace(/(?:EGP|ج\.م\.?|جنيه)\s*\d+(?:\.\d+)?\s*(?:\/|لكل)\s*\S+/gi, " ");
                remText = remText.replace(/\d+(?:\.\d+)?\s*(?:EGP|ج\.م\.?|جنيه)\s*(?:\/|لكل)\s*\S+/gi, " ");

                let currPrices = [];
                const reg = /(?:EGP|ج\.م\.?|جنيه)\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:EGP|ج\.م\.?|جنيه)/gi;
                let m;
                while ((m = reg.exec(remText)) !== null) {
                    let v = parseFloat(m[1] || m[2]);
                    if (v && v >= 2 && !currPrices.includes(v)) currPrices.push(v);
                }

                let title = (img.alt && img.alt.trim().length > 6) ? img.alt.trim() : "";
                if (!title) {
                    const lines = (bestCard.innerText || "").split(/\r?\n/).map(l => l.trim()).filter(l => {
                        if (l.length < 6) return false;
                        return !/^(?:EGP|ج\.م|جنيه|\d+(\.\d+)?$|-?\d+%|خصم|وفر|إضافة|أضف|Add)/i.test(arToEn(l));
                    });
                    title = lines[0] || "منتج عرض Amazon Now";
                }

                cardsMap.set(prodId, {
                    prodId,
                    asin: asinMatch ? asinMatch[1] : "",
                    imgId,
                    href,
                    title,
                    currPrices,
                    oldPrices
                });
            }
        }
    });

    return Array.from(cardsMap.values());
}
"""

SCROLL_JS = """
() => {
    window.scrollBy(0, 900);
    const candidates = Array.from(document.querySelectorAll("main, [role='main'], div"));
    for (const el of candidates) {
        if (el.scrollHeight > el.clientHeight + 300) {
            const st = window.getComputedStyle(el);
            if (st.overflowY === "auto" || st.overflowY === "scroll") {
                el.scrollBy(0, 900);
            }
        }
    }
}
"""

ASIN_IMG_RE_1 = re.compile(r'"asin"\s*:\s*"(B[A-Z0-9]{9})"[\s\S]{0,500}?/images/I/([A-Za-z0-9+%-]+?)\.', re.I)
ASIN_IMG_RE_2 = re.compile(r'/images/I/([A-Za-z0-9+%-]+?)\.[\s\S]{0,500}?"asin"\s*:\s*"(B[A-Z0-9]{9})"', re.I)

def collect_asins_from_text(txt, img_to_asin):
    for m in ASIN_IMG_RE_1.finditer(txt):
        img_to_asin[m.group(2).split(".")[0]] = m.group(1)
    for m in ASIN_IMG_RE_2.finditer(txt):
        img_to_asin[m.group(1).split(".")[0]] = m.group(2)

def main():
    cookies = parse_playwright_cookies(AMAZON_COOKIES)
    if not cookies:
        print("⚠️ لم يتم العثور على AMAZON_COOKIES في Secrets، تخطي فحص Amazon Now.")
        return

    sent_deals = load_sent()
    print(f"⚡ بدء فحص Amazon Now السحابي ({len(NOW_TARGETS)} أقسام | خصم فعلي {MIN_DISCOUNT_PERCENT}%+)...")

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

        img_to_asin = {}

        def on_response(resp):
            try:
                if resp.status == 200 and ("json" in resp.headers.get("content-type", "") or "tez" in resp.url):
                    t = resp.text()
                    if "/images/I/" in t and "asin" in t.lower():
                        collect_asins_from_text(t, img_to_asin)
            except Exception:
                pass

        page.on("response", on_response)

        for idx, (cat_name, url) in enumerate(NOW_TARGETS, 1):
            print(f"\n[{idx}/{len(NOW_TARGETS)}] فحص قسم: {cat_name}...")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=35000)
                time.sleep(4)

                try:
                    collect_asins_from_text(page.content(), img_to_asin)
                except Exception:
                    pass

                body_txt = page.inner_text("body")
                if "لقد فقدت الاتصال" in body_txt:
                    print("   ⚠️ ظهرت رسالة 'لقد فقدت الاتصال' - الكوكيز تحتاج تحديث أو المخزن مغلق مؤقتاً.")
                    break

                last_count = 0
                stagnant = 0
                all_items = {}

                for _ in range(18):
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
                    time.sleep(1.3)

                if len(all_items) == 0:
                    snippet = " ".join(body_txt.split())[:120]
                    print(f"   ℹ️ تشخيص الصفحة (0 منتج): العنوان='{page.title()}' | النص='{snippet}'")

                max_disc = 0
                sent_now_cnt = 0

                for prod_id, it in all_items.items():
                    curr_prices = it.get("currPrices") or []
                    old_prices = it.get("oldPrices") or []

                    # يشترط وجود سعر قديم مشطوب وسعر حالي معاً للمقارنة الحقيقية
                    if not curr_prices or not old_prices:
                        continue

                    curr_p = min(curr_prices)
                    old_p = max(old_prices)

                    if old_p <= curr_p or old_p > curr_p * 12:
                        continue

                    # حساب نسبة الخصم الفعلية من السعرين فقط
                    disc = round(((old_p - curr_p) / old_p) * 100)

                    if disc > max_disc:
                        max_disc = disc

                    if disc >= MIN_DISCOUNT_PERCENT:
                        asin = it.get("asin") or img_to_asin.get(it.get("imgId", ""), "")
                        unique_key = asin if asin else prod_id
                        title_key = "TITLE_" + it["title"][:30]

                        prev_p = sent_deals.get(unique_key)
                        if (not prev_p or prev_p > curr_p) and (sent_deals.get(title_key) != curr_p):
                            if asin:
                                final_link = f"https://www.amazon.eg/dp/{asin}"
                            elif it.get("href") and "/explore/category" not in it["href"]:
                                final_link = it["href"]
                            else:
                                q = urllib.parse.quote_plus(it["title"])
                                final_link = f"https://www.amazon.eg/s?k={q}"

                            saved_egp = round(old_p - curr_p, 2)
                            msg = (
                                f"⚡ لقطة Amazon Now بخصم فعلي {disc}%!\n"
                                f"🏷️ القسم: {cat_name}\n\n"
                                f"📦 {it['title']}\n"
                                f"💰 السعر الجديد: {curr_p} EGP\n"
                                f"❌ السعر القديم: {old_p} EGP (وفرت {saved_egp} ج)\n\n"
                                f"🔗 {final_link}"
                            )
                            if send_telegram(msg):
                                sent_deals[unique_key] = curr_p
                                sent_deals[title_key] = curr_p
                                sent_now_cnt += 1
                                print(f"   ✅ تم إرسال عرض {unique_key} بخصم فعلي {disc}%")
                                time.sleep(0.4)

                print(f"   📊 فُحص {len(all_items)} منتج | أعلى خصم فعلي: {max_disc}% | أُرسل: {sent_now_cnt}")

            except Exception as e:
                print(f"   ❌ خطأ أثناء فحص {cat_name}: {e}")

        browser.close()

    save_sent(sent_deals)

if __name__ == "__main__":
    main()
