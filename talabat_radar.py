import os
import json
import time
import asyncio
from playwright.async_api import async_playwright

TALABAT_COOKIES = os.environ.get("TALABAT_COOKIES", "").strip()
LATITUDE = float(os.environ.get("TALABAT_LAT") or "31.2156")
LONGITUDE = float(os.environ.get("TALABAT_LNG") or "29.9553")
TEST_URL = "https://www.talabat.com/egypt/talabat-mart/poultry-meat-seafood/chicken-and-poultry"

def parse_playwright_cookies(raw_json):
    if not raw_json:
        return []
    try:
        raw_list = json.loads(raw_json)
    except Exception:
        return []
    future_exp = int(time.time()) + (180 * 86400)
    cleaned = []
    for c in raw_list:
        if not isinstance(c, dict) or not c.get("name") or c.get("value") is None:
            continue
        domain = str(c.get("domain", ".talabat.com"))
        if "talabat.com" not in domain:
            continue
        cleaned.append({
            "name": str(c["name"]),
            "value": str(c["value"]),
            "domain": domain,
            "path": c.get("path", "/"),
            "secure": bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", False)),
            "expires": future_exp
        })
    return cleaned

INSPECT_PAGINATION_JS = r"""
() => {
    const results = [];
    const all = Array.from(document.querySelectorAll("*"));
    for (const el of all) {
        const txt = (el.innerText || "").trim();
        if (txt === "2" || txt === "1" || (el.getAttribute("aria-label") || "").toLowerCase().includes("page") || (el.getAttribute("aria-label") || "").toLowerCase().includes("next")) {
            const rect = el.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0 && rect.width < 150) {
                const parent = el.parentElement;
                results.push({
                    tag: el.tagName,
                    text: txt,
                    cls: typeof el.className === "string" ? el.className : "",
                    aria: el.getAttribute("aria-label") || "",
                    role: el.getAttribute("role") || "",
                    href: el.getAttribute("href") || "",
                    rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
                    parentHTML: parent ? parent.outerHTML.slice(0, 500) : ""
                });
            }
        }
    }
    return results.slice(0, 10);
}
"""

async def main():
    cookies = parse_playwright_cookies(TALABAT_COOKIES)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            locale="ar-EG",
            viewport={"width": 1366, "height": 900},
            geolocation={"latitude": LATITUDE, "longitude": LONGITUDE},
            permissions=["geolocation"]
        )
        if cookies:
            await context.add_cookies(cookies)

        page = await context.new_page()
        await page.goto(TEST_URL, wait_until="domcontentloaded", timeout=25000)
        await asyncio.sleep(2.5)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.5)

        print("🔍 URL:", page.url)
        print("🔍 TITLE:", await page.title())
        info = await page.evaluate(INSPECT_PAGINATION_JS)
        print("🔍 PAGINATION ELEMENTS:")
        print(json.dumps(info, ensure_ascii=False, indent=2))
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
