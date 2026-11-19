import os
import re
import json
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from curl_cffi import requests as cffi_requests

# ================= الإعدادات والمتغيرات البيئية =================
TELEGRAM_TOKEN = (os.environ.get("TALABAT_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_TOKEN") or "").strip()
CHAT_ID = (os.environ.get("TALABAT_CHAT_ID") or os.environ.get("CHAT_ID") or "").strip()
TALABAT_COOKIES = os.environ.get("TALABAT_COOKIES", "").strip()

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
MAX_WORKERS = 10

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


# ================= تجهيز الكوكيز لـ curl_cffi =================
def parse_cookies_dict(raw_json):
    if not raw_json:
        return {}
    try:
        raw_list = json.loads(raw_json)
        return {
            str(c["name"]): str(c["value"])
            for c in raw_list
            if isinstance(c, dict) and c.get("name") and c.get("value") is not None
        }
    except Exception:
        return {}


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


# ================= استخراج البيانات من JSON والروابط =================
def normalize_title(title):
    return re.sub(r"\s+", " ", str(title)).strip()


def walk_talabat_json(obj, products_out, stats, discovered_urls):
    if isinstance(obj, dict):
        slug = obj.get("slug") or obj.get("url") or obj.get("href") or ""
        if isinstance(slug, str) and "/talabat-mart/" in slug and "/product/" not in slug:
            full_u = slug if slug.startswith("http") else f"https://www.talabat.com{slug}"
            clean_u = full_u.split("?")[0].split("#")[0].rstrip("/")
            after = clean_u.split("/talabat-mart/")[1] if "/talabat-mart/" in clean_u else ""
            if len([s for s in after.split("/") if s]) >= 2:
                cat_label = obj.get("name") or obj.get("title") or after.split("/")[-1].replace("-", " ").title()
                discovered_urls[clean_u] = normalize_title(cat_label)

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
                    products_out[key] = {
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
                walk_talabat_json(v, products_out, stats, discovered_urls)

    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                walk_talabat_json(item, products_out, stats, discovered_urls)


def extract_links_from_html(html, discovered_urls):
    matches = re.findall(r'href=["\']((?:https://www\.talabat\.com)?/egypt/talabat-mart/[^"\'?#]+)["\']', html)
    for m in matches:
        full_u = m if m.startswith("http") else f"https://www.talabat.com{m}"
        clean_u = full_u.rstrip("/")
        if "/product/" in clean_u:
            continue
        after = clean_u.split("/talabat-mart/")[1] if "/talabat-mart/" in clean_u else ""
        parts = [p for p in after.split("/") if p]
        if len(parts) >= 2 and clean_u not in discovered_urls:
            discovered_urls[clean_u] = parts[-1].replace("-", " ").title()


def is_ignored_category(name, url):
    s = f"{name} {url}".lower()
    return any(k.lower() in s for k in IGNORED_CATEGORIES)


def fetch_and_parse_category(cat_name, url, cookies_dict):
    products_out = {}
    stats = {"total_seen": 0}
    discovered_urls = {}
    cat_title = cat_name

    try:
        resp = cffi_requests.get(
            url,
            cookies=cookies_dict,
            impersonate="chrome124",
            headers={
                "Accept-Language": "ar-EG,ar;q=0.9,en-US;q=0.8",
                "Referer": "https://www.talabat.com/egypt/talabat-mart",
            },
            timeout=15
        )
        if resp.status_code != 200:
            return cat_title, url, products_out, stats["total_seen"], discovered_urls, f"HTTP {resp.status_code}"

        html = resp.text
        extract_links_from_html(html, discovered_urls)

        h1_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, flags=re.IGNORECASE | re.DOTALL)
        if h1_m:
            clean_h1 = re.sub(r"<[^>]+>", "", h1_m.group(1)).strip()
            if len(clean_h1) > 2:
                cat_title = clean_h1

        json_blocks = re.findall(r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>', html, flags=re.DOTALL)
        for jb in json_blocks:
            try:
                data = json.loads(jb)
                walk_talabat_json(data, products_out, stats, discovered_urls)
            except Exception:
                pass

        return cat_title, url, products_out, stats["total_seen"], discovered_urls, None

    except Exception as e:
        return cat_title, url, products_out, 0, discovered_urls, str(e)


def main():
    start_time = time.time()
    sent_deals = load_and_clean_sent()
    cookies_dict = parse_cookies_dict(TALABAT_COOKIES)

    print(f"🚀 بدء الرادار الصاروخي V5 (بدون متصفح | {MAX_WORKERS} مسارات متوازية)...")

    clean_start = START_URL.split("?")[0].split("#")[0].rstrip("/")
    visited = {clean_start}
    pending = [("القسم الافتتاحي", clean_start)]

    scanned_count = 0
    skipped_count = 0
    total_sent_all = 0
    total_products_scanned_all = 0
    seen_titles_in_run = set()

    while pending and scanned_count < MAX_CATEGORIES_LIMIT:
        batch = pending[:MAX_WORKERS]
        pending = pending[MAX_WORKERS:]

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(fetch_and_parse_category, name, url, cookies_dict): (name, url)
                for name, url in batch
            }

            for future in as_completed(futures):
                scanned_count += 1
                cat_title, url, products_out, total_seen, discovered_urls, err = future.result()

                if err:
                    print(f"[{scanned_count}] ⚠️ {cat_title}: {err}")
                    continue

                for d_url, d_name in discovered_urls.items():
                    if d_url not in visited:
                        visited.add(d_url)
                        if is_ignored_category(d_name, d_url):
                            skipped_count += 1
                        elif len(visited) < MAX_CATEGORIES_LIMIT + skipped_count:
                            pending.append((d_name, d_url))

                total_in_cat = max(total_seen, len(products_out))
                total_products_scanned_all += total_in_cat

                if total_in_cat > 0:
                    sent_deals.pop("__ALERT_TS__", None)

                max_disc = 0
                sent_cnt = 0

                for it in products_out.values():
                    norm_key = it["title"][:40].lower()
                    if norm_key in seen_titles_in_run:
                        continue
                    seen_titles_in_run.add(norm_key)

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

                        prev_p = get_saved_price(sent_deals, unique_key)
                        prev_title_p = get_saved_price(sent_deals, title_cache_key)

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
                                f"🏷️ القائمة: {cat_title}\n\n"
                                f"📦 {title_str}\n"
                                f"💰 السعر الجديد: {curr_p} EGP\n"
                                f"❌ السعر القديم: {old_p} EGP (وفرت {saved_egp} ج)"
                                f"{unit_line}"
                            )

                            buttons = [
                                [{"text": "🛒 اطلب المنتج من طلبات مارت", "url": search_link}],
                                [{"text": f"📂 تصفح قائمة {cat_title[:24]}", "url": url}]
                            ]

                            if send_telegram_deal(caption, img_url=it.get("imgUrl", ""), buttons=buttons):
                                record_deal(sent_deals, unique_key, curr_p)
                                record_deal(sent_deals, title_cache_key, curr_p)
                                sent_cnt += 1
                                total_sent_all += 1
                                tag = "🚨 تصفية" if disc >= FLASH_DISCOUNT_PERCENT else "✅ لقطة"
                                print(f"   {tag} [{disc}%]: {title_str[:40]} ({curr_p} ج)")

                print(f"[{scanned_count}] {cat_title[:26]:<26} | منتجات: {total_in_cat:<3} | مخفض: {len(products_out):<2} | أعلى خصم: {max_disc}% | أُرسل: {sent_cnt}")

    if total_products_scanned_all == 0:
        maybe_send_alert(sent_deals, "لم يتم سحب أي منتجات — تأكد من صلاحية TALABAT_COOKIES.")

    save_sent(sent_deals)
    elapsed = round(time.time() - start_time, 1)
    print(f"\n🏁 اكتمل المسح الصاروخي في {elapsed} ثانية فقط! | قوائم: {scanned_count} (تخطي {skipped_count}) | منتجات: {total_products_scanned_all} | مرسل: {total_sent_all}")


if __name__ == "__main__":
    main()
