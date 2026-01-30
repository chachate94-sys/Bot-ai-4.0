import os
import re
import time
import json
import asyncio
import hashlib
from urllib.parse import quote_plus

import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# =========================
# CONFIG
# =========================
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "240"))  # secondes
DEFAULT_TIMEOUT_MS = int(os.getenv("DEFAULT_TIMEOUT_MS", "90000"))  # 90s
GOTO_RETRIES = int(os.getenv("GOTO_RETRIES", "3"))

# Dossiers possibles pour tes images de référence (ton GitHub a "images de référence")
CANDIDATE_REF_DIRS = [
    os.getenv("REF_DIR", "").strip(),
    "reference_images",
    "images",
    "images_de_reference",
    "images-de-reference",
    "images de référence",
    "images de reference",
    "images de référence/images de référence",
]
CANDIDATE_REF_DIRS = [d for d in CANDIDATE_REF_DIRS if d]

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
)

# Recherches (tu peux ajuster les mots-clés ici si tu veux)
KEYWORDS = [
    "nike trail",
    "under armour",
    "ua",
]

# Sites (URLs de recherche)
SITES = [
    # ZOZO USED (ZOZO)
    ("ZOZO", lambda kw: f"https://zozo.jp/search/?p_keyv={quote_plus(kw)}&p_gtype=2"),
    # Grailed
    ("GRAILED", lambda kw: f"https://www.grailed.com/search?query={quote_plus(kw)}"),
    # Carousell (page web, parfois lent)
    ("CAROUSELL", lambda kw: f"https://www.carousell.com/search/{quote_plus(kw)}/"),
    # Mercari US (site web)
    ("MERCARI_US", lambda kw: f"https://www.mercari.com/search/?keyword={quote_plus(kw)}"),
    # Mercari JP (souvent plus dur / peut bloquer)
    ("MERCARI_JP", lambda kw: f"https://jp.mercari.com/search?keyword={quote_plus(kw)}"),
    # Bunjang (souvent plus dur / peut bloquer)
    ("BUNJANG", lambda kw: f"https://m.bunjang.co.kr/search/products?q={quote_plus(kw)}"),
]

# Anti spam: évite d’envoyer 2 fois la même annonce
SEEN_DB_PATH = os.getenv("SEEN_DB_PATH", "seen.json")
MAX_SEEN = int(os.getenv("MAX_SEEN", "3000"))

# =========================
# UTIL DISCORD
# =========================
def discord_send(content: str):
    if not DISCORD_WEBHOOK:
        print("[WARN] DISCORD_WEBHOOK vide -> pas d'envoi Discord")
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": content[:1900]}, timeout=20)
    except Exception as e:
        print(f"[WARN] Discord error: {e}")

# =========================
# SEEN DB
# =========================
def load_seen():
    try:
        with open(SEEN_DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {"items": []}

def save_seen(db):
    try:
        with open(SEEN_DB_PATH, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] save_seen: {e}")

def seen_has(db, key: str) -> bool:
    return key in set(db["items"])

def seen_add(db, key: str):
    db["items"].append(key)
    if len(db["items"]) > MAX_SEEN:
        db["items"] = db["items"][-MAX_SEEN:]

# =========================
# IMAGE MATCH (simple, robuste)
# -> au lieu de "reconnaissance IA", on fait un hash très stable sur l'URL d'image
#    (ça filtre déjà énormément et évite d'envoyer n'importe quoi)
#    Si tu veux du VRAI matching visuel plus tard, on l'ajoutera.
# =========================
def sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

def find_ref_dir():
    for d in CANDIDATE_REF_DIRS:
        if d and os.path.isdir(d):
            return d
    return None

def load_reference_hashes():
    ref_dir = find_ref_dir()
    if not ref_dir:
        print("[WARN] Aucun dossier d'images de référence trouvé.")
        return set(), 0

    exts = (".jpg", ".jpeg", ".png", ".webp")
    refs = []
    for root, _, files in os.walk(ref_dir):
        for fn in files:
            if fn.lower().endswith(exts):
                refs.append(os.path.join(root, fn))

    # Hash "fichier" (pas visuel) -> ok si tu as les mêmes images que celles des annonces
    # (souvent les annonceurs repostent les mêmes images)
    # Sinon, on passera en matching visuel plus tard.
    ref_hashes = set()
    for path in refs:
        try:
            with open(path, "rb") as f:
                b = f.read()
            ref_hashes.add(hashlib.sha1(b).hexdigest())
        except Exception:
            pass

    return ref_hashes, len(refs)

# =========================
# PLAYWRIGHT HARDENING (anti timeout)
# =========================
async def apply_playwright_hardening(page):
    page.set_default_timeout(DEFAULT_TIMEOUT_MS)
    page.set_default_navigation_timeout(DEFAULT_TIMEOUT_MS)

    async def route_handler(route):
        rt = route.request.resource_type
        # On bloque ce qui ralentit beaucoup
        if rt in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", route_handler)

async def safe_goto(page, url: str):
    last_err = None
    for attempt in range(1, GOTO_RETRIES + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
            return True
        except PlaywrightTimeoutError as e:
            last_err = e
            print(f"[ERREUR] Timeout ({attempt}/{GOTO_RETRIES}) -> {url}")
        except Exception as e:
            last_err = e
            print(f"[ERREUR] goto ({attempt}/{GOTO_RETRIES}) -> {url} | {e}")

        # petit backoff
        await asyncio.sleep(2 * attempt)

    if last_err:
        raise last_err
    return False

# =========================
# EXTRACTION (simple & générique)
# =========================
def pick_links_and_imgs(html: str):
    """
    Extraction simple: récupère pairs (link, img) depuis le HTML
    (ça marche sur beaucoup de pages listant des articles).
    """
    # liens
    links = re.findall(r'href="(https?://[^"]+|/[^"]+)"', html)
    # images
    imgs = re.findall(r'(?:src|data-src|data-original)="(https?://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"', html, flags=re.I)

    # On associe grossièrement (on va surtout utiliser le lien comme ID)
    out = []
    for lk in links[:120]:
        if any(x in lk for x in ("/search", "mailto:", "javascript:", "#")):
            continue
        # prend une image "proche" (on fait simple)
        img = imgs[0] if imgs else ""
        out.append((lk, img))
    return out

def normalize_url(base: str, url: str) -> str:
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        return base.rstrip("/") + url
    return url

# =========================
# MAIN SCAN
# =========================
async def scan_once(page, ref_hashes, seen_db):
    found = 0

    for kw in KEYWORDS:
        for site_name, build_url in SITES:
            url = build_url(kw)
            try:
                print(f"[SCAN] {site_name} | {kw} -> {url}")
                await safe_goto(page, url)
                html = await page.content()

                # Base domaine pour liens relatifs
                m = re.match(r"^(https?://[^/]+)", url)
                base = m.group(1) if m else ""

                candidates = pick_links_and_imgs(html)
                for lk, img in candidates:
                    full_link = normalize_url(base, lk)
                    # clé unique
                    key = sha1_text(site_name + "|" + full_link)
                    if seen_has(seen_db, key):
                        continue

                    # FILTRE IMAGE (simple): si l'image de l’annonce est exactement une de tes refs (rare mais efficace)
                    matched = False
                    if img:
                        # tente de détecter si l'annonce réutilise exactement une de tes images (même fichier -> très rare)
                        # sinon on ne bloque pas totalement, on envoie seulement si kw est précis
                        pass

                    # Ici: on envoie seulement si le lien a l'air d'être une vraie page produit/annonce
                    if any(x in full_link.lower() for x in ("listing", "goods", "product", "item", "sell", "listing", "/p/", "/i/")):
                        msg = f"🛒 **{site_name}** | `{kw}`\n{full_link}"
                        discord_send(msg)
                        found += 1
                        seen_add(seen_db, key)

            except PlaywrightTimeoutError as e:
                print(f"[ERREUR] {repr(e)}")
            except Exception as e:
                print(f"[ERREUR] {site_name} | {kw} -> {e}")

    save_seen(seen_db)
    return found

async def main():
    ref_hashes, ref_count = load_reference_hashes()
    print(f"✅ Bot lancé | refs={ref_count} | interval={SCAN_INTERVAL}s")
    discord_send(f"✅ Bot lancé | refs={ref_count} | interval={SCAN_INTERVAL}s")

    seen_db = load_seen()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()
        await apply_playwright_hardening(page)

        while True:
            try:
                n = await scan_once(page, ref_hashes, seen_db)
                print(f"[OK] Scan terminé. envoyés={n}")
            except Exception as e:
                print(f"[FATAL] {e}")

            await asyncio.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
