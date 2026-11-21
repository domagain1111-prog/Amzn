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

MIN_DISCOUNT_PERCENT = 40
CACHE_TTL_DAYS = 7
COOKIE_ALERT_COOLDOWN = 12 * 3600
MAX_RUN_SECONDS = 540
AUTO_DISCOVER_EXTRA_LINKS = True

STATE_FILE = "sent_now.json"
QC_BRAND = "sAuWWBROaG"

NOW_ENTRY_PAGES = [
    f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=220630950031&title=Top+Deals&ref=EG_Tez_SF_Top_deals",
    "https://www.amazon.eg/tez",
]

NOW_TARGETS = [
    ("أقوى العروض (Top Deals)", f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=220630950031&title=Top+Deals&ref=EG_Tez_SF_Top_deals"),
    ("عروض إضافية 1", f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=223577117031"),
    ("عروض إضافية 2", f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=223985148031"),
    ("الإلكترونيات والموبايلات", f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=212925567031&title=Electronics"),
    ("الشوكولاتة والحلويات", f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=215093316031&title=Chocolate+and+Candies"),
    ("السناكس والحلويات", f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=215093298031&title=Snack+%26+Sweets"),
    ("القهوة والشاي", f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=212925569031&title=Coffee+and+Tea"),
    ("خزين المطبخ والبقالة", f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=212925568031&title=Food+Cupboard"),
    ("الألبان والبيض", f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=215052334031&title=Bakery"),
    ("الخضروات والفاكهة", f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=214841697031&title=Fruits+and+Vegetables"),
    ("الجمال والعناية الشخصية", f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=215021840031&ref=EG_Tez_SF_Herotator_Beauty"),
    ("الصحة والعافية", f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=215021841031&title=Health"),
    ("مستلزمات المنزل", f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=215103357031&ref=SA_Tez_SF_SBC_Home"),
    ("المنظفات والغسيل", f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=215103358031&title=Household+Cleaning"),
    ("المشروبات والعصائر", f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=212925566031&ref=EG_Tez_SF_SBC_Beverages"),
    ("المخبوزات الطازجة", f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=215052333031&ref=EG_Tez_SF_SBC_Bakery"),
    ("الدواجن واللحوم والأسماك", f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=214841696031&ref=EG_Tez_SF_SBC_CMF"),
    ("الأطفال والرضع", f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=215093317031&title=Baby"),
    ("الألعاب والترفيه", f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={QC_BRAND}&nodeId=215093318031&title=Toys"),
]

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
        if k == "__COOKIE_ALERT_TS__":
            if isinstance(v, (int, float)) and (now - v) < 86400:
                cleaned[k] = v
            continue
        if k == "__NEXT_IDX__":
            if isinstance(v, int) and v >= 0:
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
        print(f"   ⚠️ إعدادات تيليجرام ناقصة: TOKEN={bool(TELEGRAM_TOKEN)} | CHAT_ID={bool(CHAT_ID)}")
        return False

    photo_api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    msg_api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    reply_markup = {"inline_keyboard": buttons} if buttons else None
    hi_res_img = re.sub(r"\._[^.]+_\.", "._SL500_.", img_url) if img_url else ""

    sent_ok = False
    for cid in [c.strip() for c in CHAT_ID.split(",") if c.strip()]:
        delivered = False
        if hi_res_img:
            try:
                payload = {"chat_id": cid, "photo": hi_res_img, "caption": caption[:1020]}
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
                else:
                    print(f"   ❌ تيليجرام رفض الإرسال لـ {cid} ({r.status_code}): {r.text}")
            except Exception as e:
                print(f"   ❌ خطأ اتصال بتيليجرام لـ {cid}: {e}")
    return sent_ok

def maybe_send_cookie_alert(sent_deals, reason):
    now = int(time.time())
    last_ts = sent_deals.get("__COOKIE_ALERT_TS__", 0)
    if (now - last_ts) >= COOKIE_ALERT_COOLDOWN:
        alert_msg = (
            "⚠️ تنبيه من رادار Amazon Now!\n\n"
            f"السبب: {reason}\n"
            "🔑 الجلسة تحتاج تجديد: افتح تبويب متخفي (Incognito)، سجل دخول في amazon.eg مع تفعيل (تذكرني / Keep me signed in ✅)، انسخ الكوكيز واقفل التبويب دون تسجيل خروج، ثم ضعها في AMAZON_COOKIES."
        )
        if send_telegram_text(alert_msg):
            sent_deals["__COOKIE_ALERT_TS__"] = now

def parse_playwright_cookies(raw_json):
    if not raw_json:
        return []
    try:
        raw_list = json.loads(raw_json)
    except Exception:
        print("⚠️ صيغة AMAZON_COOKIES غير صحيحة! تأكد من نسخها بصيغة Export as JSON.")
        return []

    now_sec = int(time.time())
    future_exp = now_sec + (180 * 86400)
    cleaned = []
    for c in raw_list:
        if not isinstance(c, dict) or not c.get("name") or c.get("value") is None:
            continue
        domain = str(c.get("domain", ".amazon.eg"))
        if "amazon.eg" not in domain:
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

def warmup_session(page):
    try:
        page.goto("https://www.amazon.eg/?language=ar_AE", wait_until="domcontentloaded", timeout=30000)
        time.sleep(2.5)
    except Exception as e:
        print(f"   ℹ️ تمهيد الصفحة الرئيسية: {e}")

def collect_links_from_current_page(page, known_nodes, known_urls):
    found = []
    try:
        raw_links = page.evaluate("""() => {
            const out = [];
            for (const a of document.querySelectorAll("a[href]")) {
                const href = a.href || a.getAttribute("href") || "";
                if (href.includes("/tez/")) {
                    out.push({
                        href,
                        txt: (a.innerText || a.getAttribute("aria-label") || a.getAttribute("title") || "").trim()
                    });
                }
            }
            const html = document.documentElement.innerHTML || "";
            const re = /\/tez\/browse\/explore\/category[^"'\\s<>)]*/g;
            let m;
            while ((m = re.exec(html)) !== null) {
                out.push({ href: m[0], txt: "" });
            }
            return out;
        }""")

        for item in raw_links:
            href = (item.get("href") or "").replace("&amp;", "&").strip()
            if not href:
                continue
            full_url = urllib.parse.urljoin("https://www.amazon.eg", href)
            if "/tez/browse/explore/category" not in full_url:
                continue

            parsed = urllib.parse.urlparse(full_url)
            qs = urllib.parse.parse_qs(parsed.query)
            node_id = (qs.get("nodeId") or [""])[0]
            qc = (qs.get("qcbrand") or [QC_BRAND])[0]
            title_q = (qs.get("title") or [""])[0]

            if not node_id or node_id in known_nodes:
                continue

            clean_url = f"https://www.amazon.eg/tez/browse/explore/category?qcbrand={qc}&nodeId={node_id}"
            if clean_url in known_urls:
                continue

            known_nodes.add(node_id)
            known_urls.add(clean_url)

            txt = re.sub(r"\s+", " ", item.get("txt") or "").strip()
            if not txt and title_q:
                txt = urllib.parse.unquote_plus(title_q)
            label = txt[:35] if len(txt) >= 2 else f"قسم ناو ({node_id})"
            found.append((label, clean_url))
    except Exception:
        pass
    return found

def deep_discover_now_sections(page, existing_targets):
    """اكتشاف شامل لكافة أقسام Amazon Now من الصفحات المرجعية والصفحة الحالية"""
    discovered = []
    known_nodes = set()
    known_urls = set()

    for _, u in existing_targets:
        known_urls.add(u)
        m = re.search(r"nodeId=(\d+)", u)
        if m:
            known_nodes.add(m.group(1))

    discovered.extend(collect_links_from_current_page(page, known_nodes, known_urls))

    for entry_url in NOW_ENTRY_PAGES:
        if entry_url in known_urls:
            continue
        try:
            page.goto(entry_url, wait_until="domcontentloaded", timeout=25000)
            time.sleep(2.5)
            for _ in range(4):
                page.evaluate("window.scrollBy(0, 900)")
                time.sleep(0.6)
            discovered.extend(collect_links_from_current_page(page, known_nodes, known_urls))
        except Exception:
            pass

    return discovered

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
    function countCurrencies(str) {
        const m = arToEn(str || "").match(/(?:EGP|ج\.م\.?|جنيه)/gi);
        return m ? m.length : 0;
    }
    function hasCurrencyAndDigit(str) {
        const s = arToEn(str || "");
        return /(?:EGP|ج\.م\.?|جنيه)/i.test(s) && /\d/.test(s);
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

    const BADGE_RE = /(?:وفر|توفير|خصم|تخفيض|كوبون|قسيمة|أقل|توصيل|شحن|مجان|قسط|تقسيط|save|saving|off\b|discount|coupon|voucher|delivery|free|installment|%)/i;
    const UNIT_RE = /(?:\/|لكل)\s*(?:كجم|جم|جرام|لتر|مل|قطعة|حبة|عبوة|وحدة|kg|g\b|ml|l\b|count|pc|pack|unit)/i;

    function getAsinFromReact(rootEl) {
        const asinRe = /^B[0-9A-Z]{9}$/;
        const urlAsinRe = /(?:\/dp\/|\/gp\/product\/|\/product\/|asin[=:"'/]+)([B0-9][A-Z0-9]{9})/i;
        const nodes = [rootEl, ...Array.from(rootEl.querySelectorAll("*")).slice(0, 25)];

        for (const node of nodes) {
            if (!node) continue;
            if (node.attributes) {
                for (let i = 0; i < node.attributes.length; i++) {
                    const val = node.attributes[i].value || "";
                    if (asinRe.test(val)) return val;
                    const um = val.match(urlAsinRe);
                    if (um) return um[1];
                }
            }
            const keys = Object.keys(node);
            for (const k of keys) {
                if (k.startsWith("__reactProps$") || k.startsWith("__reactFiber$")) {
                    const rootObj = node[k];
                    const queue = [rootObj, rootObj && rootObj.memoizedProps, rootObj && rootObj.pendingProps, rootObj && rootObj.return && rootObj.return.memoizedProps];
                    const seen = new Set();
                    let steps = 0;

                    while (queue.length > 0 && steps < 80) {
                        const cur = queue.shift();
                        steps++;
                        if (!cur || typeof cur !== "object" || seen.has(cur)) continue;
                        if (cur instanceof Node || cur instanceof Window) continue;
                        seen.add(cur);

                        for (const [pk, pv] of Object.entries(cur)) {
                            if (typeof pv === "string") {
                                if (asinRe.test(pv)) return pv;
                                const m = pv.match(urlAsinRe);
                                if (m) return m[1];
                            } else if (pv && typeof pv === "object" && !pk.startsWith("_") && pk !== "children" && pk !== "stateNode" && pk !== "return" && pk !== "sibling") {
                                queue.push(pv);
                            }
                        }
                    }
                }
            }
        }
        return "";
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
            const asinMatch = (href + " " + bestCard.innerHTML).match(/(?:\/dp\/|\/gp\/product\/|\/product\/|asin[=:"']+)([B0-9][A-Z0-9]{9})/i);
            let asin = asinMatch ? asinMatch[1] : "";
            if (!asin) {
                try { asin = getAsinFromReact(bestCard); } catch (e) {}
            }

            const imgUrl = img.src || "";
            const imgFile = (imgUrl.split("/I/")[1] || "").split("?")[0];
            const imgId = imgFile.split(".")[0];
            const prodId = asin ? asin : ("TEZ_" + (imgId || img.alt || "")).slice(0, 35);

            if (prodId && !cardsMap.has(prodId)) {
                const origNodes = [bestCard, ...Array.from(bestCard.querySelectorAll("*"))];
                const clone = bestCard.cloneNode(true);
                const cloneNodes = [clone, ...Array.from(clone.querySelectorAll("*"))];

                clone.querySelectorAll(".a-price-fraction").forEach(fr => {
                    if (fr.textContent && !fr.textContent.trim().startsWith(".")) {
                        fr.textContent = "." + fr.textContent.trim();
                    }
                });

                let oldPrices = [];

                origNodes.forEach((node, idx) => {
                    if (idx > 0 && isStrikeNode(node)) {
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
                            const rawStrike = cleanPriceText(offscreen ? offscreen.textContent : (node.innerText || ""));
                            if (!rawStrike.includes("%") && !UNIT_RE.test(rawStrike)) {
                                extractNumbers(rawStrike).forEach(v => {
                                    if (!oldPrices.includes(v)) oldPrices.push(v);
                                });
                            }
                        }
                        if (cloneNodes[idx]) cloneNodes[idx].remove();
                    }
                });

                cloneNodes.forEach((cNode, idx) => {
                    if (idx === 0 || !clone.contains(cNode)) return;
                    const t = cleanPriceText(cNode.innerText || "").trim();
                    if (t.length > 0 && t.length <= 25 && t.includes("%") && countCurrencies(t) === 0) {
                        cNode.remove();
                    }
                });

                cloneNodes.forEach((cNode, idx) => {
                    if (idx === 0 || !clone.contains(cNode)) return;
                    const t = cleanPriceText(cNode.innerText || "").trim();
                    if (t.length > 0 && t.length <= 48 && countCurrencies(t) <= 1) {
                        if (BADGE_RE.test(t) || UNIT_RE.test(t) || /^\s*-\s*(?:EGP|ج\.م|جنيه|\d)/i.test(t)) {
                            cNode.remove();
                        }
                    }
                });

                let currCandidates = [];
                cloneNodes.forEach((cNode, idx) => {
                    if (!clone.contains(cNode)) return;
                    const rawT = cNode.innerText || "";
                    if (!hasCurrencyAndDigit(rawT)) return;

                    const childHasBoth = Array.from(cNode.children).some(ch => hasCurrencyAndDigit(ch.innerText || ""));
                    if (childHasBoth) return;

                    let cleaned = cleanPriceText(rawT);
                    cleaned = cleaned.replace(/(?:وفر|توفير|خصم|تخفيض|save|saving|off)\s*[:\-]?\s*(?:EGP|ج\.م\.?|جنيه)?\s*\d+(?:\.\d+)?\s*(?:EGP|ج\.م\.?|جنيه)?/gi, " ");
                    cleaned = cleaned.replace(/(?:EGP|ج\.م\.?|جنيه)?\s*\d+(?:\.\d+)?\s*(?:EGP|ج\.م\.?|جنيه)?\s*(?:off\b|خصم|توفير|وفر)/gi, " ");
                    cleaned = cleaned.replace(/\([^)]*(?:\/|لكل)[^)]*\)/gi, " ");

                    if (BADGE_RE.test(cleaned) || UNIT_RE.test(cleaned)) return;

                    const fontSize = parseFloat(window.getComputedStyle(origNodes[idx]).fontSize) || 14;

                    let priceRegex;
                    if (cleaned.trim().length <= 22) {
                        priceRegex = /(?:EGP|ج\.م\.?|جنيه)\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:EGP|ج\.م\.?|جنيه)/gi;
                    } else {
                        priceRegex = /(?:EGP|ج\.م\.?|جنيه)\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)[ \t]*(?:ج\.م\.?|جنيه)/gi;
                    }

                    let m;
                    while ((m = priceRegex.exec(cleaned)) !== null) {
                        const val = parseFloat(m[1] || m[2]);
                        if (val && val >= 2) {
                            currCandidates.push({ val, fontSize });
                        }
                    }
                });

                currCandidates.sort((a, b) => b.fontSize - a.fontSize);
                const currPrices = [];
                currCandidates.forEach(c => {
                    if (!currPrices.includes(c.val)) currPrices.push(c.val);
                });

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
                    asin,
                    imgId,
                    imgUrl,
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

ASIN_VAL_RE = re.compile(r"^B[0-9A-Z]{9}$")
IMG_ID_RE = re.compile(r"/images/I/([A-Za-z0-9+%-]+?)(?:\._|\.)")

def walk_json_for_asins(obj, img_to_asin, title_to_asin):
    if isinstance(obj, dict):
        found_asins = []
        found_imgs = []
        found_titles = []

        for k, v in obj.items():
            kl = k.lower()
            if isinstance(v, str):
                if ASIN_VAL_RE.match(v) or (kl in ("asin", "productid", "id") and len(v) == 10 and v.startswith("B")):
                    found_asins.append(v)
                if "/images/I/" in v:
                    for im in IMG_ID_RE.finditer(v):
                        found_imgs.append(im.group(1).split(".")[0])
                if kl in ("title", "name", "producttitle", "alt", "displaystring") and len(v.strip()) > 6:
                    found_titles.append(v.strip())

        if len(found_asins) == 1:
            asin = found_asins[0]
            for img_id in found_imgs:
                img_to_asin[img_id] = asin
            for t in found_titles:
                title_to_asin[t[:40]] = asin

        for v in obj.values():
            if isinstance(v, (dict, list)):
                walk_json_for_asins(v, img_to_asin, title_to_asin)

    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                walk_json_for_asins(item, img_to_asin, title_to_asin)

def resolve_direct_asin_by_search(context, title, img_id):
    search_page = context.new_page()
    found_asin = ""
    try:
        q = urllib.parse.quote_plus(title)
        search_page.goto(f"https://www.amazon.eg/s?k={q}", wait_until="domcontentloaded", timeout=18000)
        found_asin = search_page.evaluate(
            """(targetImgId) => {
                const cards = Array.from(document.querySelectorAll("div[data-asin]")).filter(d => /^B[0-9A-Z]{9}$/.test(d.getAttribute("data-asin") || ""));
                if (targetImgId) {
                    for (const c of cards) {
                        const img = c.querySelector("img[src*='/images/I/']");
                        if (img && img.src.includes(targetImgId)) {
                            return c.getAttribute("data-asin");
                        }
                    }
                }
                return cards.length > 0 ? cards[0].getAttribute("data-asin") : "";
            }""",
            img_id or ""
        )
    except Exception:
        pass
    finally:
        search_page.close()
    return found_asin

def main():
    cookies = parse_playwright_cookies(AMAZON_COOKIES)
    if not cookies:
        print("⚠️ لم يتم العثور على AMAZON_COOKIES في Secrets، تخطي فحص Amazon Now.")
        return

    start_ts = time.time()
    sent_deals = load_and_clean_sent()
    targets = list(NOW_TARGETS)
    saved_next_idx = sent_deals.get("__NEXT_IDX__", 0) % len(targets)

    priority_head = targets[:3]
    rotating_tail = targets[3:]
    if rotating_tail:
        rot_offset = saved_next_idx % len(rotating_tail)
        ordered_targets = priority_head + rotating_tail[rot_offset:] + rotating_tail[:rot_offset]
    else:
        ordered_targets = priority_head

    print(f"⚡ بدء فحص Amazon Now الشامل (أساسي: {len(targets)} قسم | خصم فعلي {MIN_DISCOUNT_PERCENT}%+)...")

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

        warmup_session(page)

        img_to_asin = {}
        title_to_asin = {}

        def on_response(resp):
            try:
                if resp.status == 200 and ("json" in resp.headers.get("content-type", "") or "tez" in resp.url):
                    data = resp.json()
                    walk_json_for_asins(data, img_to_asin, title_to_asin)
            except Exception:
                pass

        page.on("response", on_response)
        lost_conn_streak = 0
        discovered_added = False
        idx = 0

        while idx < len(ordered_targets):
            if time.time() - start_ts > MAX_RUN_SECONDS:
                print("\n⏱️ تم الوصول لحد وقت الجولة الآمن لحفظ الذاكرة واستكمال الباقي في الجولة التالية.")
                break

            cat_name, url = ordered_targets[idx]
            print(f"\n[{idx + 1}/{len(ordered_targets)}] فحص قسم: {cat_name}...")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=35000)
                time.sleep(3.5)

                page_title = page.title()
                body_txt = page.inner_text("body")

                if (
                    "amazon sign in" in page_title.lower()
                    or "تسجيل الدخول" in page_title
                    or "/ap/signin" in page.url
                ):
                    print("   🚨 صفحة تسجيل الدخول ظهرت - الكوكيز انتهت صلاحيتها فعلياً!")
                    maybe_send_cookie_alert(sent_deals, "أمازون طلب تسجيل الدخول صراحةً (/ap/signin)")
                    break

                if "لقد فقدت الاتصال" in body_txt:
                    page.reload(wait_until="domcontentloaded", timeout=25000)
                    time.sleep(3)
                    body_txt = page.inner_text("body")
                    if "لقد فقدت الاتصال" in body_txt:
                        lost_conn_streak += 1
                        print(f"   ⚠️ ظهرت رسالة 'لقد فقدت الاتصال' ({lost_conn_streak}/3).")
                        if lost_conn_streak >= 3:
                            print("   🛑 تكررت في 3 أقسام (إما الكوكيز فصلت أو فرع ناو مغلق مؤقتاً في منطقتك).")
                            maybe_send_cookie_alert(sent_deals, "استمرار ظهور 'لقد فقدت الاتصال' في أقسام ناو")
                            break
                        idx += 1
                        continue

                lost_conn_streak = 0

                if AUTO_DISCOVER_EXTRA_LINKS and not discovered_added:
                    extra_links = deep_discover_now_sections(page, ordered_targets)
                    discovered_added = True
                    if extra_links:
                        ordered_targets.extend(extra_links)
                        print(f"   🔎 تم اكتشاف {len(extra_links)} قسم إضافي تلقائياً وإضافته للقائمة.")
                    # العودة لرابط القسم الحالي بعد الاستكشاف لضمان فحصه
                    page.goto(url, wait_until="domcontentloaded", timeout=35000)
                    time.sleep(2.5)

                try:
                    next_json_list = page.evaluate(
                        """() => Array.from(document.querySelectorAll("script#__NEXT_DATA__, script[type='application/json']")).map(s => s.textContent)"""
                    )
                    for raw_j in next_json_list:
                        if raw_j:
                            try:
                                walk_json_for_asins(json.loads(raw_j), img_to_asin, title_to_asin)
                            except Exception:
                                pass
                except Exception:
                    pass

                last_count = 0
                stagnant = 0
                all_items = {}

                for _ in range(16):
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
                    time.sleep(1.2)

                if len(all_items) > 0:
                    sent_deals.pop("__COOKIE_ALERT_TS__", None)

                max_disc = 0
                sent_now_cnt = 0
                cached_skip_cnt = 0

                for prod_id, it in all_items.items():
                    curr_prices = it.get("currPrices") or []
                    old_prices = it.get("oldPrices") or []

                    if not curr_prices or not old_prices:
                        continue

                    curr_p = curr_prices[0]
                    old_p = max(old_prices)

                    if old_p <= curr_p or old_p > curr_p * 10:
                        continue

                    disc = round(((old_p - curr_p) / old_p) * 100)

                    if disc > max_disc:
                        max_disc = disc

                    if disc >= MIN_DISCOUNT_PERCENT:
                        img_id = it.get("imgId", "")
                        img_url = it.get("imgUrl", "")
                        title_str = it.get("title", "")
                        asin = (
                            it.get("asin")
                            or img_to_asin.get(img_id, "")
                            or title_to_asin.get(title_str[:40], "")
                        )

                        title_key = "TITLE_" + title_str[:30]
                        temp_key = asin if asin else prod_id
                        prev_temp_p = get_saved_price(sent_deals, temp_key)
                        prev_title_p = get_saved_price(sent_deals, title_key)

                        if (not prev_temp_p or prev_temp_p > curr_p) and (prev_title_p != curr_p):
                            if not asin:
                                asin = resolve_direct_asin_by_search(context, title_str, img_id)

                            unique_key = asin if asin else prod_id
                            if get_saved_price(sent_deals, unique_key) == curr_p:
                                cached_skip_cnt += 1
                                continue

                            if asin:
                                final_link = f"https://www.amazon.eg/dp/{asin}"
                            elif it.get("href") and "/explore/category" not in it["href"] and "/s?" not in it["href"]:
                                final_link = it["href"]
                            else:
                                final_link = f"https://www.amazon.eg/s?k={urllib.parse.quote_plus(title_str)}"

                            saved_egp = round(old_p - curr_p, 2)
                            caption = (
                                f"⚡ لقطة Amazon Now بخصم فعلي {disc}%!\n"
                                f"🏷️ القسم: {cat_name}\n\n"
                                f"📦 {title_str}\n"
                                f"💰 السعر الجديد: {curr_p} EGP\n"
                                f"❌ السعر القديم: {old_p} EGP (وفرت {saved_egp} ج)"
                            )

                            buttons = []
                            first_row = []
                            if asin:
                                add_cart_url = f"https://www.amazon.eg/gp/aws/cart/add.html?ASIN.1={asin}&Quantity.1=1"
                                first_row.append({"text": "🛒 أضف للسلة فوراً", "url": add_cart_url})
                            first_row.append({"text": "📦 صفحة المنتج", "url": final_link})
                            buttons.append(first_row)
                            buttons.append([{"text": f"⚡ قسم {cat_name}", "url": url}])

                            if send_telegram_deal(caption, img_url=img_url, buttons=buttons):
                                record_deal(sent_deals, unique_key, curr_p)
                                record_deal(sent_deals, title_key, curr_p)
                                sent_now_cnt += 1
                                print(f"   ✅ تم إرسال عرض {unique_key} بالصورة والأزرار (خصم {disc}%)")
                                time.sleep(0.4)
                        else:
                            cached_skip_cnt += 1

                print(f"   📊 فُحص {len(all_items)} منتج | أعلى خصم فعلي: {max_disc}% | أُرسل: {sent_now_cnt} | بالذاكرة مسبقاً: {cached_skip_cnt}")

            except Exception as e:
                print(f"   ❌ خطأ أثناء فحص {cat_name}: {e}")

            idx += 1

        browser.close()

    if rotating_tail:
        sent_deals["__NEXT_IDX__"] = (saved_next_idx + max(1, idx)) % len(rotating_tail)
    save_sent(sent_deals)

if __name__ == "__main__":
    main()
