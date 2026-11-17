import os
import re
import json
import time
import random
import requests
from bs4 import BeautifulSoup
from urllib.parse import unquote
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
MIN_DISCOUNT_PERCENT = 40
STATE_FILE = "sent_deals.json"

WAREHOUSE_URLS = [
    "https://www.amazon.eg/s?k=%D8%A8%D8%A7%D9%88%D8%B1+%D8%A8%D8%A7%D9%86%D9%83&i=warehouse-deals&srs=27972173031",
    "https://www.amazon.eg/s?k=%D8%B3%D8%A7%D8%B9%D8%A9&i=warehouse-deals&srs=27972173031",
    "https://www.amazon.eg/s?k=%D8%A7%D9%8A%D8%B1%D8%A8%D9%88%D8%AF%D8%B2&i=warehouse-deals&srs=27972173031",
    "https://www.amazon.eg/s?k=%D8%B4%D8%A7%D8%B4%D9%87&i=warehouse-deals&srs=27972173031",
    "https://www.amazon.eg/s?k=Tv&i=warehouse-deals&srs=27972173031",
    "https://www.amazon.eg/s?k=%D9%85%D8%A8%D8%B1%D8%AF+%D9%85%D9%8A%D8%A7%D9%87&i=warehouse-deals&srs=27972173031",
    "https://www.amazon.eg/s?k=%D8%AB%D9%84%D8%A7%D8%AC%D8%A9&i=warehouse-deals&srs=27972173031",
    "https://link.amazon/B0eFldugz",
    "https://link.amazon/B0fel50x6",
    "https://link.amazon/B095coxM0",
    "https://link.amazon/B06aAKHT4"
]

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
        v = clean_price(el.get_text())
        if v and v > 20 and v not in prices:
            prices.append(v)
    txt = item.get_text(" ", strip=True)
    reg = r"(?:EGP|ج\.م\.?|جنيه)\s*([\d,٠-٩]+(?:\.[\d٠-٩]+)?)|([\d,٠-٩]+(?:\.[\d٠-٩]+)?)\s*(?:EGP|ج\.م\.?|جنيه)"
    for m in re.finditer(reg, txt, re.I):
        v = clean_price(m.group(1) or m.group(2))
        if v and v > 20 and v not in prices:
            prices.append(v)
    return prices

def get_original_price(page, asin, used_price):
    try:
        page.goto(f"https://www.amazon.eg/dp/{asin}", wait_until="domcontentloaded", timeout=25000)
        soup = BeautifulSoup(page.content(), "html.parser")
        candidates = []
        selectors = [
            "#newAccordionRow_0 .a-price .a-offscreen",
            "#newAccordionRow .a-price .a-offscreen",
            "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
            "#corePrice_feature_div .a-price .a-offscreen",
            "#centerCol .basisPrice .a-price .a-offscreen"
        ]
        for sel in selectors:
            for el in soup.select(sel):
                if el.find_parent(id=["usedBuySection", "usedAccordionRow"]):
                    continue
                v = clean_price(el.get_text())
                if v and used_price < v < used_price * 8:
                    candidates.append(v)
        return max(candidates) if candidates else None
    except Exception:
        return None

def main():
    state = load_state()
    sent_deals = state.get("sent", {})
    ref_cache = state.get("ref", {})

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            locale="ar-EG",
            viewport={"width": 1366, "height": 768}
        )
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        try:
            page.goto("https://www.amazon.eg/", wait_until="domcontentloaded", timeout=25000)
            time.sleep(2)
        except Exception:
            pass

        for idx, target_url in enumerate(WAREHOUSE_URLS, 1):
            print(f"[{idx}/{len(WAREHOUSE_URLS)}] فحص: {target_url}")
            try:
                if "link.amazon" in target_url:
                    r = requests.get(target_url, allow_redirects=False, timeout=15)
                    loc = r.headers.get("Location", target_url)
                    if loc.startswith("intent://"):
                        fb = re.search(r"browser_fallback_url=([^;]+)", loc)
                        target_url = unquote(fb.group(1)) if fb else "https://" + loc[9:].split("#Intent")[0]
                    else:
                        target_url = loc

                page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(random.uniform(2, 4))
                html = page.content()

                if "captchacharacters" in html:
                    print("   ⚠️ أمازون طلب كابتشا في هذا الرابط، تخطي مؤقت.")
                    continue

                soup = BeautifulSoup(html, "html.parser")
                items = soup.select("div[data-component-type='s-search-result']")
                print(f"   تم العثور على {len(items)} منتج.")

                for item in items:
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
                            old_p = get_original_price(page, asin, used_p)
                            ref_cache[asin] = {"price": old_p or used_p, "ts": time.time()}
                            time.sleep(random.uniform(1, 2))

                    if old_p and old_p > used_p:
                        disc = round(((old_p - used_p) / old_p) * 100)
                        if disc >= MIN_DISCOUNT_PERCENT:
                            prev_sent = sent_deals.get(asin)
                            if not prev_sent or prev_sent > used_p:
                                t_el = item.select_one("h2 span") or item.select_one("h2")
                                title = t_el.get_text(strip=True) if t_el else "منتج مستودع أمازون"
                                msg = (
                                    f"🔥 لقطة سحابية بخصم {disc}% (مستودع أمازون)!\n\n"
                                    f"📦 {title}\n"
                                    f"💰 سعر اللقطة: {used_p} EGP\n"
                                    f"❌ السعر الأصلي: {old_p} EGP\n\n"
                                    f"🔗 https://www.amazon.eg/dp/{asin}"
                                )
                                if send_telegram(msg):
                                    sent_deals[asin] = used_p
                                    print(f"   ✅ تم إرسال لقطة {asin} بخصم {disc}%")

            except Exception as e:
                print(f"   ❌ خطأ: {e}")

        browser.close()

    state["sent"] = sent_deals
    state["ref"] = ref_cache
    save_state(state)

if __name__ == "__main__":
    main()
