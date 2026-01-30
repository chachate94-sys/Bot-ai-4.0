import os
import json
import time
import asyncio
import hashlib
from dataclasses import dataclass
from typing import List, Optional

import aiohttp
from PIL import Image
import imagehash
from playwright.async_api import async_playwright


REF_DIR = "reference_images"
SEEN_FILE = "seen.json"
DEFAULT_CONFIG_PATH = "config.json"


@dataclass
class Listing:
    site: str
    title: str
    url: str
    image_urls: List[str]


def load_config() -> dict:
    # 1) Railway: tu mets le JSON dans CONFIG_JSON (recommandé)
    cfg_env = os.getenv("CONFIG_JSON")
    if cfg_env:
        return json.loads(cfg_env)

    # 2) Ou alors depuis un fichier config.json
    path = os.getenv("CONFIG_PATH", DEFAULT_CONFIG_PATH)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_seen() -> set:
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen: set) -> None:
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(list(seen))[-30000:], f)
    except Exception as e:
        print("[SEEN] save error:", e)


def stable_id(site: str, url: str) -> str:
    return hashlib.sha256(f"{site}|{url}".encode("utf-8")).hexdigest()[:24]


def load_reference_hashes() -> List[imagehash.ImageHash]:
    if not os.path.isdir(REF_DIR):
        raise RuntimeError(f"❌ Dossier '{REF_DIR}' introuvable. Crée-le et mets tes images dedans.")
    hashes = []
    for name in os.listdir(REF_DIR):
        if not name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            continue
        path = os.path.join(REF_DIR, name)
        try:
            img = Image.open(path).convert("RGB")
            hashes.append(imagehash.phash(img))
        except Exception as e:
            print(f"[REF] impossible de lire {name}: {e}")
    if not hashes:
        raise RuntimeError("❌ Aucune image trouvée dans reference_images/")
    return hashes


async def fetch_bytes(session: aiohttp.ClientSession, url: str, timeout_s: int = 15) -> Optional[bytes]:
    try:
        async with session.get(url, timeout=timeout_s) as r:
            if r.status != 200:
                return None
            return await r.read()
    except Exception:
        return None


def phash_from_bytes(data: bytes) -> Optional[imagehash.ImageHash]:
    try:
        from io import BytesIO
        img = Image.open(BytesIO(data)).convert("RGB")
        return imagehash.phash(img)
    except Exception:
        return None


def is_match(candidate: imagehash.ImageHash, refs: List[imagehash.ImageHash], threshold: int) -> bool:
    return any((candidate - r) <= threshold for r in refs)


async def discord_notify(webhook: str, listing: Listing, img_url: str, dist: int) -> None:
    if not webhook:
        print("[DISCORD] DISCORD_WEBHOOK manquant.")
        return

    content = (
        f"🚨 **MATCH IMAGE**\n"
        f"**Site :** {listing.site}\n"
        f"**Titre :** {listing.title}\n"
        f"**Distance :** {dist}\n"
        f"{listing.url}\n"
        f"{img_url}"
    )

    async with aiohttp.ClientSession() as s:
        try:
            async with s.post(webhook, json={"content": content}, timeout=15) as r:
                if r.status not in (200, 204):
                    txt = await r.text()
                    print("[DISCORD] error", r.status, txt[:200])
        except Exception as e:
            print("[DISCORD] exception:", e)


# ----------------------------
# Scrapers (Playwright) — uniquement tes sites
# ----------------------------

async def scrape_grailed(page, query: str, limit: int) -> List[Listing]:
    url = f"https://www.grailed.com/shop?query={query}"
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1600)

    out: List[Listing] = []
    seen = set()
    cards = await page.query_selector_all('a[href*="/listing/"]')
    for a in cards:
        href = await a.get_attribute("href")
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.grailed.com" + href
        if href in seen:
            continue
        seen.add(href)

        title = (await a.get_attribute("title")) or (await a.inner_text()) or "Grailed listing"
        img_urls = []
        img = await a.query_selector("img")
        if img:
            src = await img.get_attribute("src") or await img.get_attribute("data-src")
            if src:
                img_urls.append(src)

        out.append(Listing(site="Grailed", title=title.strip()[:140], url=href, image_urls=img_urls))
        if len(out) >= limit:
            break
    return out


async def scrape_mercari_us(page, query: str, limit: int) -> List[Listing]:
    url = f"https://www.mercari.com/search/?keyword={query}"
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1800)

    out: List[Listing] = []
    seen = set()
    cards = await page.query_selector_all('a[href*="/us/item/"], a[href*="/item/"]')
    for a in cards:
        href = await a.get_attribute("href")
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.mercari.com" + href
        if href in seen:
            continue
        seen.add(href)

        title = (await a.get_attribute("title")) or (await a.inner_text()) or "Mercari item"
        img_urls = []
        img = await a.query_selector("img")
        if img:
            src = await img.get_attribute("src") or await img.get_attribute("data-src")
            if src:
                img_urls.append(src)

        out.append(Listing(site="Mercari US", title=title.strip()[:140], url=href, image_urls=img_urls))
        if len(out) >= limit:
            break
    return out


async def scrape_mercari_jp(page, query: str, limit: int) -> List[Listing]:
    url = f"https://jp.mercari.com/search?keyword={query}"
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1800)

    out: List[Listing] = []
    seen = set()
    cards = await page.query_selector_all('a[href*="/item/"]')
    for a in cards:
        href = await a.get_attribute("href")
        if not href or "/item/" not in href:
            continue
        if href.startswith("/"):
            href = "https://jp.mercari.com" + href
        if href in seen:
            continue
        seen.add(href)

        title = (await a.inner_text()) or "Mercari JP item"
        img_urls = []
        img = await a.query_selector("img")
        if img:
            src = await img.get_attribute("src") or await img.get_attribute("data-src")
            if src:
                img_urls.append(src)

        out.append(Listing(site="Mercari JP", title=title.strip()[:140], url=href, image_urls=img_urls))
        if len(out) >= limit:
            break
    return out


async def scrape_bunjang_global(page, query: str, limit: int) -> List[Listing]:
    url = f"https://globalbunjang.com/search?q={query}"
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1800)

    out: List[Listing] = []
    seen = set()
    cards = await page.query_selector_all('a[href*="/product/"], a[href*="/products/"]')
    for a in cards:
        href = await a.get_attribute("href")
        if not href:
            continue
        if href.startswith("/"):
            href = "https://globalbunjang.com" + href
        if href in seen:
            continue
        seen.add(href)

        title = (await a.inner_text()) or "Bunjang item"
        img_urls = []
        img = await a.query_selector("img")
        if img:
            src = await img.get_attribute("src") or await img.get_attribute("data-src")
            if src:
                img_urls.append(src)

        out.append(Listing(site="Bunjang", title=title.strip()[:140], url=href, image_urls=img_urls))
        if len(out) >= limit:
            break
    return out


async def scrape_carousell(page, domain: str, query: str, limit: int) -> List[Listing]:
    q = query.strip().replace(" ", "%20")
    url = f"https://{domain}/{q}/q/"
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2200)

    out: List[Listing] = []
    seen = set()
    cards = await page.query_selector_all('a[href*="/p/"]')
    for a in cards:
        href = await a.get_attribute("href")
        if not href:
            continue
        if href.startswith("/"):
            href = f"https://{domain}" + href
        if href in seen:
            continue
        seen.add(href)

        title = (await a.get_attribute("title")) or (await a.inner_text()) or "Carousell item"
        img_urls = []
        img = await a.query_selector("img")
        if img:
            src = await img.get_attribute("src") or await img.get_attribute("data-src")
            if src:
                img_urls.append(src)

        out.append(Listing(site=f"Carousell ({domain})", title=title.strip()[:140], url=href, image_urls=img_urls))
        if len(out) >= limit:
            break
    return out


async def scrape_zozoused(page, query: str, limit: int) -> List[Listing]:
    q = query.strip().replace(" ", "%20")
    url = f"https://zozo.jp/search/?p_keyv={q}&p_gtype=2"
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2200)

    out: List[Listing] = []
    seen = set()
    cards = await page.query_selector_all('a[href]')
    for a in cards:
        href = await a.get_attribute("href")
        if not href:
            continue
        # on garde seulement les liens produits probables (best-effort)
        if not any(x in href for x in ("/_goods/", "/goods/", "/shop/")):
            continue
        if href.startswith("/"):
            href = "https://zozo.jp" + href
        if href in seen:
            continue
        seen.add(href)

        title = (await a.get_attribute("title")) or (await a.inner_text()) or "ZOZO item"
        img_urls = []
        img = await a.query_selector("img")
        if img:
            src = await img.get_attribute("src") or await img.get_attribute("data-src")
            if src:
                img_urls.append(src)

        if img_urls:  # sans image ça ne sert à rien pour toi
            out.append(Listing(site="ZOZOUSED/ZOZO", title=title.strip()[:140], url=href, image_urls=img_urls))
        if len(out) >= limit:
            break
    return out


# ----------------------------
# Scan loop (image-only)
# ----------------------------

async def scan_once(cfg: dict, refs: List[imagehash.ImageHash], seen: set) -> None:
    threshold = int(cfg.get("phash_distance_threshold", 8))
    limit = int(cfg.get("max_items_per_site", 25))
    enabled = cfg.get("sites_enabled", {})
    queries = cfg.get("queries", [])
    carousell_domains = cfg.get("carousell_domains", [])

    webhook = os.getenv("DISCORD_WEBHOOK", "").strip()
    # (optionnel) fallback si tu le mets dans config.json
    if not webhook:
        webhook = str(cfg.get("discord_webhook", "")).strip()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"
        )
        page = await context.new_page()

        async with aiohttp.ClientSession() as session:
            for q in queries:
                q = q.strip()
                if not q:
                    continue

                listings: List[Listing] = []

                if enabled.get("mercari_jp", True):
                    listings += await scrape_mercari_jp(page, q, limit)

                if enabled.get("mercari_us", True):
                    listings += await scrape_mercari_us(page, q, limit)

                if enabled.get("bunjang_global", True):
                    listings += await scrape_bunjang_global(page, q, limit)

                if enabled.get("carousell", True):
                    per_domain = max(8, limit // max(1, len(carousell_domains)))
                    for d in carousell_domains:
                        listings += await scrape_carousell(page, d, q, per_domain)

                if enabled.get("zozoused", True):
                    listings += await scrape_zozoused(page, q, limit)

                if enabled.get("grailed", True):
                    listings += await scrape_grailed(page, q, limit)

                # IMAGE-ONLY matching
                for lst in listings:
                    uid = stable_id(lst.site, lst.url)
                    if uid in seen:
                        continue

                    best_dist = 999
                    best_img = None

                    for img_url in lst.image_urls[:3]:
                        data = await fetch_bytes(session, img_url)
                        if not data:
                            continue
                        ph = phash_from_bytes(data)
                        if not ph:
                            continue
                        # calcule la meilleure distance vs tes refs
                        for rh in refs:
                            d = ph - rh
                            if d < best_dist:
                                best_dist = d
                                best_img = img_url

                    # On alerte uniquement si match
                    if best_img is not None and best_dist <= threshold:
                        print(f"[MATCH] {lst.site} dist={best_dist} {lst.title} {lst.url}")
                        await discord_notify(webhook, lst, best_img, best_dist)

                    seen.add(uid)

        await context.close()
        await browser.close()

    save_seen(seen)


async def main():
    cfg = load_config()
    refs = load_reference_hashes()
    seen = load_seen()

    interval = int(cfg.get("scan_interval_seconds", 240))
    print(f"✅ Bot lancé | refs={len(refs)} | interval={interval}s")

    while True:
        try:
            await scan_once(cfg, refs, seen)
        except Exception as e:
            print("[ERREUR]", repr(e))
        time.sleep(max(60, interval))


if __name__ == "__main__":
    asyncio.run(main())
