import os
import re
import json
import time
import random
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs, urlencode
from playwright.sync_api import sync_playwright

# ======== الإعدادات الأساسية ========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
MIN_DISCOUNT_PERCENT = 40   # أقل نسبة خصم مطلوبة (40%)
MIN_USED_PRICE = 150        # تجاهل الإكسسوارات الرخيصة جداً أقل من 150 جنيه
STATE_FILE = "sent_deals.json"
MAX_RUN_SECONDS = 540

# البوابة الرئيسية لمستودع أمازون مصر (Amazon Resale) مرتبة بـ "وصل حديثاً"
WAREHOUSE_MAIN_URL = "https://www.amazon.eg/s?i=warehouse-deals&srs=27972173031&s=date-desc-rank"

# أقسام احتياطية شاملة داخل المستودع مرتبة بـ "وصل حديثاً" لضمان عدم تفويت أي جهاز
FALLBACK_FEEDS = [
    ("وصل حديثاً - موبايلات وتابلت", "https://www.amazon.eg/s?k=%D9%85%D9%88%D8%A8%D8%A7%D9%8A%D9%84&i=warehouse-deals&srs=27972173031&s=date-desc-rank"),
    ("وصل حديثاً - لابتوب وكمبيوتر", "https://www.amazon.eg/s?k=%D9%84%D8%A7%D8%A8+%D8%AA%D9%88%D8%A8&i=warehouse-deals&srs=27972173031&s=date-desc-rank"),
    ("وصل حديثاً - شاشات وتلفزيونات", "https://www.amazon.eg/s?k=%D8%B4%D8%A7%D8%B4%D8%A9&i=warehouse-deals&srs=27972173031&s=date-desc-rank"),
    ("وصل حديثاً - إلكترونيات وسماعات وساعات", "https://www.amazon.eg/s?k=%D8%B3%D9%85%D8%A7%D8%B9%D8%A9&i=warehouse-deals&srs=27972173031&s=date-desc-rank"),
    ("وصل حديثاً - أجهزة منزلية ومطبخ", "https://www.amazon.eg/s?k=%D8%A3%D8%AC%D9%87%D8%B2%D8%A9+%D9%85%D9%86%D8%B2%D9%84%D9%8A%D8%A9&i=warehouse-deals&srs=27972173031&s=date-desc-rank"),
    ("وصل حديثاً - ثلاجات وغسالات وتكييف", "https://www.amazon.eg/s?k=%D8%AB%D9%84%D8%A7%D8%AC%D8%A9&i=warehouse-deals&srs=27972173031&s=date-desc-rank"),
]

# كلمات الأقسام المهمة لاختيارها تلقائياً من القائمة الجانبية للمستودع
TARGET_DEPT_KEYWORDS = [
    "إلكترونيات", "الكترونيات", "موبايل", "هواتف", "اتصالات", "كمبيوتر", "حاسب",
    "لابتوب", "أجهزة منزلية", "اجهزة منزلية", "منزل", "مطبخ", "تلفزيون", "صوت",
    "electronics", "mobile", "phone", "computer", "laptop", "appliance", "home", "kitchen", "tv"
]

# كلمات لاستبعاد المنتجات غير المهمة أو الإكسسوارات التافهة
EXCLUDE_PRODUCT_KEYWORDS = [
    "كتاب", "رواية", "ملابس", "بنطلون", "قميص", "تيشيرت", "فستان", "حذاء", "شراب",
    "مكياج", "روج", "كريم", "شامبو", "حفاضات", "لعبة أطفال", "طعام قطط", "طعام كلاب",
    "جراب", "كفر موبايل", "اسكرينة", "لاصقة شاشة", "حماية شاشة",
    "screen protector", "phone case", "silicone case", "t-shirt", "lipstick", "shampoo", "diaper"
]
# ====================================

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"sent": {}, "ref": {}}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=15)
        return r.status_code == 200
    except Exception:
        return False

def force_newest_sort(url):
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    qs["s"] = ["date-desc-rank"]
    new_query = urlencode(qs, doseq=True)
    return f"https://www.amazon.eg{parsed.path}?{new_query}"

def is_important_product(title):
    t = title.lower()
    for bad in EXCLUDE_PRODUCT_KEYWORDS:
        if bad in t:
            return False
    return True

def set_egypt_delivery(page):
    """ضبط عنوان التوصيل إلى القاهرة حتى تظهر الأجهزة المنزلية والموبايلات والبطاريات للسيرفر"""
    print("📍 جاري ضبط عنوان التوصيل داخل مصر (القاهرة)...")
    try:
        page.goto("https://www.amazon.eg/", wait_until="domcontentloaded", timeout=25000)
        time.sleep(2)

        loc_btn = page.locator("#nav-global-location-popover-link")
        if loc_btn.count() > 0:
            loc_btn.first.click(timeout=5000)
            time.sleep(2.5)

            city_sel = page.locator("select#GLUXCityList, select[id*='City']")
            if city_sel.count() > 0:
                options = city_sel.first.locator("option").all_inner_texts()
                target_idx = 1
                for idx, opt in enumerate(options):
                    if "القاهرة" in opt or "Cairo" in opt:
                        target_idx = idx
                        break
                if len(options) > 1:
                    city_sel.first.select_option(index=target_idx)
                    time.sleep(1.5)

                dist_sel = page.locator("select#GLUXDistrictList, select[id*='District']")
                if dist_sel.count() > 0:
                    d_opts = dist_sel.first.locator("option").all_inner_texts()
                    if len(d_opts) > 1:
                        dist_sel.first.select_option(index=1)
                        time.sleep(1)

                done_btn = page.locator("button[name='glowDoneButton'], #GLUXConfirmClose, .a-popover-footer button")
                if done_btn.count() > 0:
                    done_btn.first.click(timeout=4000)
                    time.sleep(2)

        page.reload(wait_until="domcontentloaded", timeout=20000)
        time.sleep(1.5)
        loc_txt = page.locator("#glow-ingress-line2").inner_text(timeout=3000).strip()
        print(f"   ✅ موقع التوصيل الحالي على أمازون: {loc_txt}")
    except Exception as e:
        print(f"   ℹ️ استمرار الفحص الافتراضي: {e}")

def discover_warehouse_departments(page):
    """استخراج روابط الأقسام المهمة مباشرة من مستودع أمازون مع تفعيل وصل حديثاً"""
    feeds = [("المستودع العام - وصل حديثاً", WAREHOUSE_MAIN_URL)]
    seen_urls = {WAREHOUSE_MAIN_URL}

    try:
        page.goto(WAREHOUSE_MAIN_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2.5)
        soup = BeautifulSoup(page.content(), "html.parser")

        for a in soup.select("#departments a, #s-refinements a, .s-navigation-item a"):
            name = a.get_text(" ", strip=True)
            href = a.get("href", "")
            if not name or not href:
                continue
            if any(k in name.lower() for k in TARGET_DEPT_KEYWORDS):
                full_url = force_newest_sort(urljoin("https://www.amazon.eg", href))
                if full_url not in seen_urls:
                    seen_urls.add(full_url)
                    feeds.append((f"قسم {name} (وصل حديثاً)", full_url))
    except Exception as e:
        print(f"   ⚠️ تعذر قراءة القائمة الجانبية: {e}")

    # إضافة الأقسام الشاملة لضمان تغطية الموبايلات والأجهزة المنزلية 100%
    for label, f_url in FALLBACK_FEEDS:
        if f_url not in seen_urls:
            seen_urls.add(f_url)
            feeds.append((label, f_url))

    return feeds

def clean_price(txt):
    if not txt:
        return None
    ar, en = "٠١٢٣٤٥٦٧٨٩", "0123456789"
    s = txt.translate(str.maketrans(ar, en))
    m = re.search(r"[\d,]+(?:\.\d+)?", s)
    return float(m.group(0).replace(",", "")) if m else None

def extract_card_prices(item):
    prices = []
    for el in item.select(".a-price .a-offscreen, .a-color-price"):
        if el.find_parent(attrs={"data-cy": "delivery-recipe"}):
            continue
        v = clean_price(el.get_text())
        if v and v >= MIN_USED_PRICE and v not in prices:
            prices.append(v)

    for row in item.select(".a-row, span"):
        t = row.get_text(" ", strip=True)
        if any(w in t.lower() for w in ["delivery", "shipping", "توصيل", "شحن", "قسط", "شهر", "month"]):
            continue
        reg = r"(?:EGP|ج\.م\.?|جنيه)\s*([\d,٠-٩]+(?:\.[\d٠-٩]+)?)|([\d,٠-٩]+(?:\.[\d٠-٩]+)?)\s*(?:EGP|ج\.م\.?|جنيه)"
        for m in re.finditer(reg, t, re.I):
            v = clean_price(m.group(1) or m.group(2))
            if v and v >= MIN_USED_PRICE and v not in prices:
                prices.append(v)
    return prices

def get_original_price(context, page, asin, used_price):
    candidates = []
    selectors = [
        "#newAccordionRow_0 .a-price .a-offscreen",
        "#newAccordionRow .a-price .a-offscreen",
        "#apex_desktop_newAccordionRow .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#corePrice_feature_div .a-price .a-offscreen",
        "#centerCol .basisPrice .a-price .a-offscreen",
        "#centerCol span[data-a-strike='true'] .a-offscreen"
    ]

    try:
        resp = context.request.get(f"https://www.amazon.eg/dp/{asin}", timeout=15000)
        html = resp.text() if resp.ok else ""
        if not html or "captchacharacters" in html:
            page.goto(f"https://www.amazon.eg/dp/{asin}", wait_until="domcontentloaded", timeout=20000)
            html = page.content()

        soup = BeautifulSoup(html, "html.parser")
        for sel in selectors:
            for el in soup.select(sel):
                if el.find_parent(id=["usedBuySection", "usedAccordionRow"]):
                    continue
                v = clean_price(el.get_text())
                if v and used_price < v < used_price * 6:
                    candidates.append(v)

        olp = soup.select_one("#olpLinkWidget_feature_div, #dynamic-aod-ingress-box")
        if olp:
            m = re.search(r"(?:New|جديد)[\s\S]{0,45}?(?:EGP|ج\.م\.?|جنيه)\s*([\d,٠-٩]+(?:\.[\d٠-٩]+)?)", olp.get_text(" ", strip=True), re.I)
            if m:
                v = clean_price(m.group(1))
                if v and used_price < v < used_price * 6:
                    candidates.append(v)
    except Exception:
        pass

    if not candidates:
        try:
            aod_url = f"https://www.amazon.eg/gp/product/ajax?asin={asin}&pc=dp&experienceId=aodAjaxMain"
            resp2 = context.request.get(aod_url, timeout=15000)
            if resp2.ok:
                soup2 = BeautifulSoup(resp2.text(), "html.parser")
                for offer in soup2.select("#aod-pinned-offer, #aod-offer"):
                    head = offer.select_one("#aod-offer-heading")
                    cond = head.get_text(" ", strip=True).lower() if head else ""
                    if ("new" in cond or "جديد" in cond) and "used" not in cond and "مستعمل" not in cond:
                        p_el = offer.select_one(".a-price .a-offscreen, .aok-offscreen")
                        v = clean_price(p_el.get_text()) if p_el else None
                        if v and used_price < v < used_price * 6:
                            candidates.append(v)
        except Exception:
            pass

    return min(candidates) if candidates else None

def main():
    start_ts = time.time()
    state = load_state()
    sent_deals = state.get("sent", {})
    ref_cache = state.get("ref", {})
    seen_in_run = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            locale="ar-EG",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={"Accept-Language": "ar-EG,ar;q=0.9,en-US;q=0.8"}
        )
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        set_egypt_delivery(page)
        feeds = discover_warehouse_departments(page)
        print(f"📂 إجمالي قوائم الفحص المستهدفة (وصل حديثاً): {len(feeds)}")

        for idx, (label, target_url) in enumerate(feeds, 1):
            if time.time() - start_ts > MAX_RUN_SECONDS:
                print("⏳ انتهى وقت هذه الجولة، تم حفظ الذاكرة بنجاح.")
                break

            print(f"[{idx}/{len(feeds)}] فحص: {label}")
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(random.uniform(2, 3.5))
                html = page.content()

                if "captchacharacters" in html:
                    print("   ⚠️ كابتشا مؤقتة، تخطي للقسم التالي.")
                    continue

                soup = BeautifulSoup(html, "html.parser")
                items = soup.select("div[data-component-type='s-search-result']")
                print(f"   تم العثور على {len(items)} منتج بالصفحة.")

                priced_cnt = 0
                max_disc = 0

                for item in items:
                    if time.time() - start_ts > MAX_RUN_SECONDS:
                        break

                    asin = (item.get("data-asin") or "").strip()
                    if len(asin) < 5 or asin in seen_in_run:
                        continue
                    seen_in_run.add(asin)

                    t_el = item.select_one("h2 span") or item.select_one("h2")
                    title = t_el.get_text(strip=True) if t_el else "جهاز مستودع أمازون"

                    if not is_important_product(title):
                        continue

                    prices = extract_card_prices(item)
                    if not prices:
                        continue

                    used_p = min(prices)
                    old_p = max(prices) if len(prices) >= 2 else None

                    if not old_p:
                        cached = ref_cache.get(asin)
                        if cached and (time.time() - cached["ts"] < 21600):
                            old_p = cached["price"]
                        else:
                            old_p = get_original_price(context, page, asin, used_p)
                            ref_cache[asin] = {"price": old_p or used_p, "ts": time.time()}
                            time.sleep(random.uniform(0.4, 0.8))

                    if old_p and old_p > used_p:
                        priced_cnt += 1
                        disc = round(((old_p - used_p) / old_p) * 100)
                        if disc > max_disc:
                            max_disc = disc

                        if disc >= MIN_DISCOUNT_PERCENT:
                            prev_sent = sent_deals.get(asin)
                            if not prev_sent or prev_sent > used_p:
                                msg = (
                                    f"🔥 وصل حديثاً بخصم {disc}% في مستودع أمازون!\n"
                                    f"🏷️ القسم: {label}\n\n"
                                    f"📦 {title}\n"
                                    f"💰 سعر اللقطة: {used_p} EGP\n"
                                    f"❌ سعر الجديد: {old_p} EGP\n\n"
                                    f"🔗 https://www.amazon.eg/dp/{asin}"
                                )
                                if send_telegram(msg):
                                    sent_deals[asin] = used_p
                                    print(f"   ✅ تم إرسال لقطة {asin} بخصم {disc}%")

                print(f"   📊 تمت مقارنة {priced_cnt} جهاز مهم | أعلى خصم حالي: {max_disc}%")

                state["sent"] = sent_deals
                state["ref"] = ref_cache
                save_state(state)

            except Exception as e:
                print(f"   ❌ خطأ: {e}")

        browser.close()

    state["sent"] = sent_deals
    state["ref"] = ref_cache
    save_state(state)

if __name__ == "__main__":
    main()
