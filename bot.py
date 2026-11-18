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

MIN_DISCOUNT_PERCENT = 50         # الخصم الأساسي للموبايلات وباقي الأقسام (50%+)
WATCHES_MIN_DISCOUNT = 70         # الخصم المخصص للساعات الكلاسيك (70%+)

STATE_FILE = "sent_deals.json"
MAX_RUN_SECONDS = 680
MAX_PAGES_PER_URL = 8

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

WATCH_KEYWORDS = (
    "ساعة يد", "ساعه يد", "ساعات يد", "ساعة رجالي", "ساعة حريمي", "ساعة نسائية",
    "ساعه رجالي", "ساعه حريمي", "كرونوغراف", "كوارتز", "أنالوج", "انالوج",
    "wristwatch", "watches", "chronograph", "quartz", "analog watch",
    "casio", "كاسيو", "fossil", "فوسيل", "seiko", "سيكو", "citizen", "سيتيزن",
    "tommy hilfiger", "تومي هيلفيغر", "curren", "كورين", "naviforce", "نافي فورس",
    "alba", "ألبا", "orient", "أورينت", "guess watch", "invicta", "timex", "swatch",
    "tissot", "تيسو", "daniel klein", "دانيال كلاين", "mini focus", "skmei"
)

SMARTWATCH_EXCEPTIONS = (
    "ساعة ذكية", "ساعه ذكيه", "سمارت", "smartwatch", "smart watch",
    "apple watch", "galaxy watch", "band", "سوار ذكي", "امازفيت", "amazfit",
    "huawei watch", "xiaomi watch", "redmi watch", "oraimo watch", "mibro", "haylou"
)

WATCH_ACCESSORY_WORDS = (
    "أستيك", "استيك", "سير ساعة", "حزام ساعة", "اسكرينة", "حافظة ساعة",
    "watch strap", "watch band", "screen protector", "watch case", "charger", "شاحن ساعة"
)

def is_classic_watch_item(title, page_heading="", current_url=""):
    full_text = f"{title} {page_heading}".lower()
    title_low = (title or "").lower()
    if any(sw in title_low for sw in SMARTWATCH_EXCEPTIONS):
        return False
    if any(acc in title_low for acc in WATCH_ACCESSORY_WORDS):
        return False
    if any(wk in full_text for wk in WATCH_KEYWORDS):
        return True
    if re.search(r"\bwatch\b", title_low):
        return True
    if "watch" in unquote(current_url).lower() or "ساعات" in unquote(current_url):
        return True
    return False

def make_title_key(title):
    if not title:
        return ""
    norm = re.sub(r"[^\w\u0600-\u06FF]+", "", title.lower())
    return f"TTL_{norm[:42]}" if len(norm) >= 8 else ""

def load_state():
    now = time.time()
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("sent", {})
                    data.setdefault("ref", {})
                    data.setdefault("next_idx", 0)
                    data["ref"] = {
                        k: v for k, v in data["ref"].items()
                        if isinstance(v, dict) and (now - v.get("ts", 0) < 86400)
                    }
                    return data
        except Exception:
            pass
    return {"sent": {}, "ref": {}, "next_idx": 0}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def extract_prev_record(entry):
    if entry is None:
        return None, 0
    if isinstance(entry, (int, float)):
        return float(entry), 0
    if isinstance(entry, dict):
        return float(entry.get("price", float("inf"))), int(entry.get("disc", 0))
    return None, 0

def should_send_deal(sent_deals, asin, title, used_p, disc):
    """
    قفل مزدوج ضد التكرار (بالـ ASIN + اسم المنتج):
    إذا أُرسل من قبل، لا يُرسل مجدداً إلا إذا انخفض السعر فعلياً بنسبة 5% على الأقل
    وبفارق لا يقل عن 25 جنيهاً، وزادت نسبة الخصم عن المرة السابقة.
    """
    t_key = make_title_key(title)
    candidates = [sent_deals.get(asin)]
    if t_key:
        candidates.append(sent_deals.get(t_key))

    known_prices = []
    known_discs = []
    for entry in candidates:
        p, d = extract_prev_record(entry)
        if p is not None:
            known_prices.append(p)
            known_discs.append(d)

    if not known_prices:
        return True

    lowest_prev_price = min(known_prices)
    highest_prev_disc = max(known_discs)

    # منع الإرسال لو السعر لم ينزل هبوطاً حقيقياً معتبراً (على الأقل 5% وأقل بـ 25 جنيه)
    significant_drop = (lowest_prev_price - used_p) >= max(25.0, lowest_prev_price * 0.05)
    bigger_discount = (highest_prev_disc == 0) or (disc >= highest_prev_disc + 2)

    return significant_drop and bigger_discount

def record_sent_deal(sent_deals, asin, title, used_p, disc):
    now_ts = int(time.time())
    keys = [asin]
    t_key = make_title_key(title)
    if t_key:
        keys.append(t_key)

    for k in keys:
        prev_p, prev_d = extract_prev_record(sent_deals.get(k))
        best_p = min(prev_p, used_p) if prev_p is not None else used_p
        best_d = max(prev_d, disc)
        sent_deals[k] = {"price": round(best_p, 2), "disc": best_d, "ts": now_ts}

def send_telegram_deal(caption, img_url="", buttons=None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(f"   ❌ خطأ إعدادات: التوكن موجود؟ {bool(TELEGRAM_TOKEN)} | الشات ID موجود؟ {bool(CHAT_ID)}")
        return False

    photo_api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    msg_api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    reply_markup = {"inline_keyboard": buttons} if buttons else None
    hi_res_img = re.sub(r"\._[^.]+_\.", "._SL500_.", img_url) if img_url else ""

    chat_ids = [cid.strip() for cid in CHAT_ID.split(",") if cid.strip()]
    sent_any = False

    for cid in chat_ids:
        delivered = False
        if hi_res_img:
            try:
                payload = {"chat_id": cid, "photo": hi_res_img, "caption": caption[:1020]}
                if reply_markup:
                    payload["reply_markup"] = reply_markup
                r = requests.post(photo_api, json=payload, timeout=15)
                if r.status_code == 200:
                    delivered = True
                    sent_any = True
            except Exception:
                pass

        if not delivered:
            try:
                payload = {"chat_id": cid, "text": caption, "disable_web_page_preview": False}
                if reply_markup:
                    payload["reply_markup"] = reply_markup
                r = requests.post(msg_api, json=payload, timeout=15)
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
    except Exception as e:
        print(f"   ℹ️ تخطي إعداد الموقع: {e}")

def clean_price(txt):
    if not txt:
        return None
    ar, en = "٠١٢٣٤٥٦٧٨٩", "0123456789"
    s = txt.translate(str.maketrans(ar, en)).replace("٬", ",")
    m = re.search(r"[\d,]+(?:\.\d+)?", s)
    return float(m.group(0).replace(",", "")) if m else None

BADGE_SKIP_WORDS = (
    "delivery", "shipping", "توصيل", "شحن", "قسط", "شهر", "month",
    "وفر", "توفير", "خصم", "كوبون", "save", "saving", "coupon", "off", "%"
)

def extract_card_prices(item):
    used_candidates = []
    strike_candidates = []

    for el in item.select(".a-price .a-offscreen, .a-color-price"):
        if el.find_parent(attrs={"data-cy": "delivery-recipe"}):
            continue
        parent_txt = el.parent.get_text(" ", strip=True).lower() if el.parent else ""
        if any(w in parent_txt for w in BADGE_SKIP_WORDS):
            continue

        v = clean_price(el.get_text())
        if not v or v <= 40:
            continue

        is_strike = bool(el.find_parent(attrs={"data-a-strike": "true"}) or el.find_parent("s") or el.find_parent(class_="a-text-price"))
        if is_strike:
            if v not in strike_candidates:
                strike_candidates.append(v)
        else:
            if v not in used_candidates:
                used_candidates.append(v)

    for row in item.select(".a-row, span"):
        if row.find("span") is not None and len(row.get_text(strip=True)) > 45:
            continue
        t = row.get_text(" ", strip=True)
        tl = t.lower()
        if any(w in tl for w in BADGE_SKIP_WORDS):
            continue
        reg = r"(?:EGP|ج\.م\.?|جنيه)\s*([\d,٠-٩]+(?:\.[\d٠-٩]+)?)|([\d,٠-٩]+(?:\.[\d٠-٩]+)?)\s*(?:EGP|ج\.م\.?|جنيه)"
        for m in re.finditer(reg, t, re.I):
            v = clean_price(m.group(1) or m.group(2))
            if v and v > 40 and v not in used_candidates and v not in strike_candidates:
                used_candidates.append(v)

    used_p = min(used_candidates) if used_candidates else None
    old_p = max(strike_candidates) if strike_candidates else (max(used_candidates) if len(used_candidates) >= 2 else None)
    return used_p, old_p

def get_original_price(context, page, asin, used_price):
    candidates = []
    selectors = [
        "#newAccordionRow_0 .a-price .a-offscreen",
        "#newAccordionRow .a-price .a-offscreen",
        "#apex_desktop_newAccordionRow .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price:not([data-a-strike='true']) .a-offscreen",
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
                if v and used_price < v < used_price * 8:
                    candidates.append(v)

        olp = soup.select_one("#olpLinkWidget_feature_div, #dynamic-aod-ingress-box")
        if olp:
            m = re.search(r"(?:New|جديد)[\s\S]{0,45}?(?:EGP|ج\.م\.?|جنيه)\s*([\d,٠-٩]+(?:\.[\d٠-٩]+)?)", olp.get_text(" ", strip=True), re.I)
            if m:
                v = clean_price(m.group(1))
                if v and used_price < v < used_price * 8:
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
                        if v and used_price < v < used_price * 8:
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

    ordered_indices = [(start_idx + i) % total_urls for i in range(total_urls)]
    priority_order = [0, 1] + [i for i in ordered_indices if i not in (0, 1)]
    seen_this_run = set()  # منع أي تكرار داخل نفس الجولة حتى لو المنتج ظهر في رابطين مختلفين

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

        set_egypt_location(page)
        print(f"📂 الفحص: {total_urls} قوائم | عام: {MIN_DISCOUNT_PERCENT}%+ | ساعات: {WATCHES_MIN_DISCOUNT}%+ | مسجل بالذاكرة: {len(sent_deals)}")
        time_up = False

        for step_num, url_idx in enumerate(priority_order, 1):
            if time.time() - start_ts > MAX_RUN_SECONDS:
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
                    page_title_txt = soup.title.get_text(" ", strip=True) if soup.title else ""
                    dept_el = soup.select_one("#departments .a-text-bold, .s-breadcrumb .a-text-bold")
                    page_heading = f"{page_title_txt} {dept_el.get_text(' ', strip=True) if dept_el else ''}"

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
                        if len(asin) < 5 or asin in seen_this_run:
                            continue
                        seen_this_run.add(asin)

                        used_p, old_p = extract_card_prices(item)
                        if not used_p:
                            continue

                        t_el = item.select_one("h2 span") or item.select_one("h2")
                        title = t_el.get_text(strip=True) if t_el else "منتج مستودع أمازون"

                        is_watch = is_classic_watch_item(title, page_heading, current_url)
                        required_disc = WATCHES_MIN_DISCOUNT if is_watch else MIN_DISCOUNT_PERCENT

                        if not old_p or old_p <= used_p:
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

                            if disc >= required_disc and should_send_deal(sent_deals, asin, title, used_p, disc):
                                img_el = item.select_one("img.s-image") or item.select_one("img[src*='/images/I/']")
                                img_url = img_el.get("src", "") if img_el else ""

                                saved_egp = round(old_p - used_p, 2)
                                prev_p, _ = extract_prev_record(sent_deals.get(asin) or sent_deals.get(make_title_key(title)))
                                drop_note = f"\n📉 نزل أكتر! (كان بـ {prev_p} EGP)" if prev_p else ""

                                watch_tag = "⌚ [لقطة ساعات 70%+] " if is_watch else "🔥 "
                                caption = (
                                    f"{watch_tag}لقطة مستودع أمازون بخصم فعلي {disc}%!{drop_note}\n"
                                    f"📄 رابط ({url_idx + 1}) - صفحة ({page_num})\n\n"
                                    f"📦 {title}\n"
                                    f"💰 سعر المستعمل: {used_p} EGP\n"
                                    f"❌ سعر الجديد: {old_p} EGP (وفرت {saved_egp} ج)"
                                )

                                dp_url = f"https://www.amazon.eg/dp/{asin}"
                                used_offers_url = f"https://www.amazon.eg/gp/offer-listing/{asin}/ref=dp_olp_used?ie=UTF8&condition=used"
                                add_cart_url = f"https://www.amazon.eg/gp/aws/cart/add.html?ASIN.1={asin}&Quantity.1=1"

                                buttons = [
                                    [{"text": "🏷️ عروض المستعمل مباشرة", "url": used_offers_url}],
                                    [
                                        {"text": "📦 صفحة المنتج", "url": dp_url},
                                        {"text": "🛒 أضف للسلة", "url": add_cart_url}
                                    ]
                                ]

                                if send_telegram_deal(caption, img_url=img_url, buttons=buttons):
                                    record_sent_deal(sent_deals, asin, title, used_p, disc)
                                    state["sent"] = sent_deals
                                    save_state(state)  # حفظ فوري بعد كل رسالة مباشرة
                                    print(f"      ✅ تم إرسال وحفظ لقطة {asin} (خصم {disc}%)")

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

            print(f"   📊 ملخص الرابط ({url_idx + 1}): تمت مقارنة {priced_cnt} منتج | أعلى خصم: {max_disc}%")

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
