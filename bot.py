import os
import re
import json
import time
import random
import requests
from bs4 import BeautifulSoup
from urllib.parse import unquote, urljoin
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
MIN_DISCOUNT_PERCENT = 40
STATE_FILE = "sent_deals.json"
MAX_RUN_SECONDS = 560       # أمان قبل انتهاء وقت GitHub Actions
MAX_PAGES_PER_URL = 8       # أقصى عدد صفحات يفحصها داخل كل رابط (تقدر تغيره براحتك)

# قسم الموبايلات (من 700 ج وطالع) + الـ 12 رابط المستهدفة
WAREHOUSE_URLS = [
    "https://www.amazon.eg/s?k=%D9%85%D9%88%D8%A8%D8%A7%D9%8A%D9%84&rh=n%3A27972173031%2Cp_36%3A70000-&s=date-desc-rank",
    "https://www.amazon.eg/s?k=Samsung+iPhone+Xiaomi+Realme+Oppo+Nokia+Infinix&rh=n%3A27972173031%2Cp_36%3A70000-&s=date-desc-rank",
    "https://link.amazon/B09NtHO69",
    "https://link.amazon/B02tVS8LW",
    "https://link.amazon/B02vpENB3",
    "https://link.amazon/B09vgf8ax",
    "https://link.amazon/B0eqLZNuv",
    "https://link.amazon/B0hw0iKbO",
    "https://link.amazon/B01ltdZun",
    "https://link.amazon/B0eFldugz",
    "https://link.amazon/B0fel50x6",
    "https://link.amazon/B095coxM0",
    "https://link.amazon/B06aAKHT4",
    "https://link.amazon/B0ePt9Jdp"
]

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("sent", {})
                    data.setdefault("ref", {})
                    data.setdefault("next_idx", 0)
                    return data
        except Exception:
            pass
    return {"sent": {}, "ref": {}, "next_idx": 0}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(f"   ❌ خطأ إعدادات: التوكن موجود؟ {bool(TELEGRAM_TOKEN)} | الشات ID موجود؟ {bool(CHAT_ID)}")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    chat_ids = [cid.strip() for cid in CHAT_ID.split(",") if cid.strip()]
    sent_any = False

    for cid in chat_ids:
        try:
            r = requests.post(url, json={"chat_id": cid, "text": text}, timeout=15)
            if r.status_code == 200:
                sent_any = True
            else:
                print(f"   ❌ تيليجرام رفض الإرسال لـ {cid} ({r.status_code}): {r.text}")
        except Exception as e:
            print(f"   ❌ خطأ اتصال بتيليجرام لـ {cid}: {e}")

    return sent_any

def resolve_short_url(url):
    cur = url.strip()
    for _ in range(5):
        if cur.startswith("intent://"):
            fb = re.search(r"browser_fallback_url=([^;]+)", cur)
            return unquote(fb.group(1)) if fb else "https://" + cur[9:].split("#Intent")[0]
        if "link.amazon" not in cur and "amzn." not in cur:
            break
        try:
            r = requests.get(cur, allow_redirects=False, timeout=15)
            loc = r.headers.get("Location")
            if not loc:
                break
            cur = loc
        except Exception:
            break
    if cur.startswith("intent://"):
        fb = re.search(r"browser_fallback_url=([^;]+)", cur)
        return unquote(fb.group(1)) if fb else "https://" + cur[9:].split("#Intent")[0]
    return cur

def get_next_page_url(soup):
    next_el = soup.select_one(
        "a.s-pagination-next:not(.s-pagination-disabled), "
        ".a-pagination li.a-last:not(.a-disabled) a"
    )
    if next_el and next_el.get("href"):
        return urljoin("https://www.amazon.eg", next_el.get("href"))
    return None

def set_egypt_location(page):
    print("📍 جاري ضبط عنوان التوصيل داخل مصر برمجياً...")
    try:
        page.goto("https://www.amazon.eg/", wait_until="domcontentloaded", timeout=25000)
        time.sleep(1.5)

        ok = page.evaluate("""async () => {
            try {
                const tokenEl = document.querySelector('meta[name="anti-csrftoken-a2z"]');
                const token = tokenEl ? tokenEl.getAttribute('content') : '';
                const headers = {
                    'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
                    'X-Requested-With': 'XMLHttpRequest'
                };
                if (token) headers['anti-csrftoken-a2z'] = token;

                const body = new URLSearchParams({
                    locationType: 'CITY',
                    city: 'Cairo',
                    cityName: 'Cairo',
                    state: 'Cairo Governorate',
                    countryCode: 'EG',
                    deviceType: 'web',
                    pageType: 'Gateway',
                    actionSource: 'glow'
                });

                const r = await fetch('/portal-migration/hz/glow/address-change?actionSource=glow', {
                    method: 'POST',
                    headers: headers,
                    body: body.toString()
                });
                return r.ok;
            } catch (e) {
                return false;
            }
        }""")
        if ok:
            page.reload(wait_until="domcontentloaded", timeout=20000)
            time.sleep(1)
            print("   ✅ تم تفعيل التوصيل داخل مصر بنجاح!")
        else:
            print("   ℹ️ تم تهيئة الجلسة الافتراضية لمصر.")
    except Exception as e:
        print(f"   ℹ️ تخطي إعداد الموقع: {e}")

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
        if v and v > 40 and v not in prices:
            prices.append(v)

    for row in item.select(".a-row, span"):
        t = row.get_text(" ", strip=True)
        if any(w in t.lower() for w in ["delivery", "shipping", "توصيل", "شحن", "قسط", "شهر", "month"]):
            continue
        reg = r"(?:EGP|ج\.م\.?|جنيه)\s*([\d,٠-٩]+(?:\.[\d٠-٩]+)?)|([\d,٠-٩]+(?:\.[\d٠-٩]+)?)\s*(?:EGP|ج\.م\.?|جنيه)"
        for m in re.finditer(reg, t, re.I):
            v = clean_price(m.group(1) or m.group(2))
            if v and v > 40 and v not in prices:
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
    total_urls = len(WAREHOUSE_URLS)
    start_idx = state.get("next_idx", 0) % total_urls

    # ترتيب القائمة بحيث يبدأ من حيث توقف في الجولة السابقة ليفحص كل الصفحات بعدل
    ordered_indices = [(start_idx + i) % total_urls for i in range(total_urls)]
    # ضمان وجود قسمي الموبايلات (0 و 1) في أول كل جولة دائماً لسرعة اقتناصها
    priority_order = [0, 1] + [i for i in ordered_indices if i not in (0, 1)]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            locale="ar-EG",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={
                "Accept-Language": "ar-EG,ar;q=0.9,en-US;q=0.8"
            }
        )
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        set_egypt_location(page)

        print(f"📂 إجمالي قوائم الفحص: {total_urls} (فحص حتى {MAX_PAGES_PER_URL} صفحات لكل رابط)")
        time_up = False

        for step_num, url_idx in enumerate(priority_order, 1):
            if time.time() - start_ts > MAX_RUN_SECONDS:
                print(f"⏳ انتهى وقت هذه الجولة، سيتم استكمال الباقي في الجولة القادمة من الرابط رقم {url_idx + 1}...")
                state["next_idx"] = url_idx
                time_up = True
                break

            raw_url = WAREHOUSE_URLS[url_idx]
            current_url = resolve_short_url(raw_url)
            print(f"\n[{step_num}/{len(priority_order)}] فحص الرابط ({url_idx + 1}): {raw_url}")

            priced_cnt = 0
            max_disc = 0
            page_num = 1

            while current_url and page_num <= MAX_PAGES_PER_URL:
                if time.time() - start_ts > MAX_RUN_SECONDS:
                    state["next_idx"] = url_idx
                    time_up = True
                    break

                try:
                    page.goto(current_url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(random.uniform(2.0, 3.2))
                    html = page.content()

                    if "captchacharacters" in html:
                        print(f"   ⚠️ كابتشا في صفحة {page_num}، تخطي الرابط مؤقتاً.")
                        break

                    soup = BeautifulSoup(html, "html.parser")
                    items = soup.select("div[data-component-type='s-search-result']")
                    print(f"   📄 صفحة ({page_num}): تم العثور على {len(items)} منتج.")

                    if not items:
                        break

                    for item in items:
                        if time.time() - start_ts > MAX_RUN_SECONDS:
                            state["next_idx"] = url_idx
                            time_up = True
                            break

                        asin = (item.get("data-asin") or "").strip()
                        if len(asin) < 5:
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
                                time.sleep(random.uniform(0.35, 0.8))

                        if old_p and old_p > used_p:
                            priced_cnt += 1
                            disc = round(((old_p - used_p) / old_p) * 100)
                            if disc > max_disc:
                                max_disc = disc

                            if disc >= MIN_DISCOUNT_PERCENT:
                                prev_sent = sent_deals.get(asin)
                                if not prev_sent or prev_sent > used_p:
                                    t_el = item.select_one("h2 span") or item.select_one("h2")
                                    title = t_el.get_text(strip=True) if t_el else "منتج مستودع أمازون"
                                    msg = (
                                        f"🔥 لقطة سحابية بخصم {disc}% (مستودع أمازون)!\n"
                                        f"📄 رابط ({url_idx + 1}) - صفحة ({page_num})\n\n"
                                        f"📦 {title}\n"
                                        f"💰 سعر اللقطة: {used_p} EGP\n"
                                        f"❌ السعر الأصلي: {old_p} EGP\n\n"
                                        f"🔗 https://www.amazon.eg/dp/{asin}"
                                    )
                                    if send_telegram(msg):
                                        sent_deals[asin] = used_p
                                        print(f"      ✅ تم إرسال لقطة {asin} بخصم {disc}%")

                    if time_up:
                        break

                    next_url = get_next_page_url(soup)
                    if next_url:
                        current_url = next_url
                        page_num += 1
                    else:
                        break

                except Exception as e:
                    print(f"   ❌ خطأ في صفحة {page_num}: {e}")
                    break

            print(f"   📊 ملخص الرابط ({url_idx + 1}): تمت مقارنة {priced_cnt} منتج عبر {page_num} صفحات | أعلى خصم: {max_disc}%")

            state["sent"] = sent_deals
            state["ref"] = ref_cache
            if not time_up:
                state["next_idx"] = (url_idx + 1) % total_urls
            save_state(state)

        browser.close()

    state["sent"] = sent_deals
    state["ref"] = ref_cache
    save_state(state)

if __name__ == "__main__":
    main()
