#!/usr/bin/env python3
# pinterest_scraper.py — practice-only scraper for public Pinterest search pages
import argparse, csv, json, os, random, re, time, pathlib, hashlib, datetime
from urllib.parse import urljoin
from typing import List, Dict, Any, Tuple
import pandas as pd
import httpx
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
]

PIN_HOST = "https://www.pinterest.com"

def jitter(a: float, b: float) -> float:
    return random.uniform(a, b)
   
def human_sleep(a: float=0.8, b: float=2.2):
    time.sleep(jitter(a,b))

def ensure_dir(p: pathlib.Path):
    p.mkdir(parents=True, exist_ok=True)

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

def extract_pin_id(href: str) -> str:
    m = re.search(r"/pin/(\d+)/", href or "")
    return m.group(1) if m else ""

# ---- IMAGE URL UPSCALING HELPERS ----
_SIZE_TOKEN_RE = re.compile(r"/\d+x/")

def to_736(url: str) -> str:
    return _SIZE_TOKEN_RE.sub("/736x/", url)

def to_originals(url: str) -> str:
    return _SIZE_TOKEN_RE.sub("/originals/", url)

def url_looks_png(url: str) -> bool:
    u = url.lower()
    return u.endswith(".png") or ".png" in u

# ---- Overlays / cookie walls ----
def best_effort_close_overlays(page):
    selectors = [
        'button[aria-label*="Not now" i]',
        'button:has-text("Not now")',
        'button[aria-label*="Close" i]',
        'button:has-text("Reject all")',
        'button:has-text("Accept all")',
        'button:has-text("Accept")',
        '[data-test-id*="close" i]',
        'div[role="dialog"] button',
    ]
    for _ in range(2):
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el:
                    el.click(timeout=800)
                    time.sleep(jitter(0.4, 0.9))
            except Exception:
                pass

def wait_for_feed(page):
    # Wait until at least one pin anchor exists (or time out)
    try:
        page.wait_for_selector('a[href^="/pin/"]', timeout=20000)
    except Exception:
        pass

# ---- CORE SCRAPER ----
def scroll_and_collect(page,
                       limit: int,
                       require_keywords: Tuple[str, ...]=(),
                       avoid_words: Tuple[str, ...]=(),
                       aspect_min: float = 0.0,
                       aspect_max: float = 10.0,
                       min_area: int = 0,
                       prefer_jpeg: bool = False) -> List[Dict[str, Any]]:
    """
    Collect pins; soft filters by default so we don't miss everything.
    You can tighten with CLI flags.
    """
    seen = set()
    items: List[Dict[str, Any]] = []
    attempts_without_new = 0

    def is_ok(row: Dict[str, Any]) -> bool:
        title = f"{row.get('title','')} {row.get('alt','')}".lower()

        # reject obvious poster/infographic language (soft list)
        bad_default = (
            "what is","tips","how to","guide","checklist","ideas","quote","quotes",
            "reasons","mistakes","rules","infographic","printable",
            "free download","worksheet","template","challenge","prompt",
            "planner","journal","to-do","tutorial"
        )
        bad = set(w.strip() for w in (avoid_words or bad_default))
        if any(w in title for w in bad):
            return False

        if require_keywords and not any(k.lower() in title for k in require_keywords):
            return False

        w, h = int(row.get("img_w") or 0), int(row.get("img_h") or 0)
        if min_area and (w*h) < min_area:
            return False
        if w > 0 and h > 0:
            ar = w / h
            if not (aspect_min <= ar <= aspect_max):
                return False

        if prefer_jpeg and url_looks_png(row.get("image_url","")):
            return False
        return True

    while len(items) < limit and attempts_without_new < 14:
        new = page.evaluate("""() => {
            const out = [];
            // Try anchors first
            const anchors = Array.from(document.querySelectorAll('a[href^="/pin/"]'));
            for (const a of anchors) {
                const href = a.getAttribute('href') || '';
                let img = a.querySelector('img') || a.parentElement?.querySelector('img');
                // backup: any image under the anchor subtree
                if (!img) {
                    const imgs = a.querySelectorAll('img');
                    if (imgs && imgs.length) img = imgs[0];
                }
                if (!img) continue;
                const src = img.currentSrc || img.src || img.getAttribute('src') || '';
                const alt = img.alt || '';
                const title = a.getAttribute('aria-label') || a.getAttribute('title') || alt || '';
                const w = img.naturalWidth || 0;
                const h = img.naturalHeight || 0;
                out.push({href, src, alt, title, w, h});
            }

            // Fallback: raw images -> nearest anchor
            const imgs = Array.from(document.querySelectorAll('img[src*="i.pinimg.com"]'));
            for (const img of imgs) {
                const src = img.currentSrc || img.src || img.getAttribute('src') || '';
                if (!src) continue;
                let el = img;
                let href = '';
                for (let i=0;i<6 && el;i++){
                    if (el.tagName === 'A' && el.getAttribute('href')?.startsWith('/pin/')) {
                        href = el.getAttribute('href');
                        break;
                    }
                    el = el.parentElement;
                }
                const alt = img.alt || '';
                const title = alt;
                const w = img.naturalWidth || 0;
                const h = img.naturalHeight || 0;
                out.push({href, src, alt, title, w, h});
            }
            return out;
        }""")

        added_now = 0
        for row in new:
            href = row.get("href") or ""
            if not href.startswith("/pin/"):
                continue
            full = urljoin(PIN_HOST, href.split("?")[0])
            img = row.get("src") or ""
            if "i.pinimg.com" not in img:
                continue

            pin_id = extract_pin_id(href)
            key = (pin_id, img)
            if key in seen:
                continue
            seen.add(key)

            candidate = {
                "pin_id": pin_id or sha1(full),
                "pin_url": full,
                "image_url": img,  # upgraded during download
                "title": (row.get("title") or "").strip(),
                "alt": (row.get("alt") or "").strip(),
                "img_w": int(row.get("w") or 0),
                "img_h": int(row.get("h") or 0),
            }
            if is_ok(candidate):
                items.append(candidate)
                added_now += 1

        attempts_without_new = attempts_without_new + 1 if added_now == 0 else 0
        if len(items) >= limit:
            break

        # Scroll + let lazy-loading populate
        page.mouse.wheel(0, random.randint(1800, 3200))
        time.sleep(jitter(1.0, 2.5))
        best_effort_close_overlays(page)

    # If still nothing, relax everything and do a last pass
    if not items:
        items_relaxed = []
        seen2 = set()
        new = page.evaluate("""() => {
            const out = [];
            const anchors = Array.from(document.querySelectorAll('a[href^="/pin/"] img'));
            for (const img of anchors) {
                const a = img.closest('a[href^="/pin/"]');
                if (!a) continue;
                const href = a.getAttribute('href') || '';
                const src = img.currentSrc || img.src || img.getAttribute('src') || '';
                const alt = img.alt || '';
                const title = a.getAttribute('aria-label') || a.getAttribute('title') || alt || '';
                out.push({href, src, alt, title, w: img.naturalWidth||0, h: img.naturalHeight||0});
            }
            return out;
        }""")
        for row in new:
            href = row.get("href") or ""
            if not href.startswith("/pin/"):
                continue
            img = row.get("src") or ""
            if "i.pinimg.com" not in img:
                continue
            full = urljoin(PIN_HOST, href.split("?")[0])
            pin_id = extract_pin_id(href) or sha1(full)
            key = (pin_id, img)
            if key in seen2:
                continue
            seen2.add(key)
            items_relaxed.append({
                "pin_id": pin_id,
                "pin_url": full,
                "image_url": img,
                "title": (row.get("title") or "").strip(),
                "alt": (row.get("alt") or "").strip(),
                "img_w": int(row.get("w") or 0),
                "img_h": int(row.get("h") or 0),
            })
        items = items_relaxed[:limit]

    # biggest first
    items.sort(key=lambda r: (r.get("img_w",0) * r.get("img_h",0)), reverse=True)
    return items[:limit]

# ---- DOWNLOADER (tries originals -> 736x -> as-is) ----
def upgraded_url_candidates(url: str, prefer_originals: bool = True) -> List[str]:
    cands = []
    if prefer_originals:
        cands.append(to_originals(url))
    cands.append(to_736(url))
    cands.append(url)
    seen = set(); ordered = []
    for u in cands:
        if u not in seen:
            ordered.append(u); seen.add(u)
    return ordered

def download_images(rows: List[Dict[str, Any]],
                    folder: pathlib.Path,
                    max_bytes: int = 25_000_000,
                    prefer_originals: bool = True):
    ensure_dir(folder)
    client = httpx.Client(timeout=40.0, headers={"User-Agent": random.choice(UA_POOL)})
    for r in rows:
        base_url = r["image_url"]
        for u in upgraded_url_candidates(base_url, prefer_originals=prefer_originals):
            name = f"{r.get('pin_id') or sha1(u)}" + (".jpg" if not url_looks_png(u) else ".png")
            dest = folder / name
            if dest.exists():
                break
            try:
                with client.stream("GET", u) as resp:
                    if resp.status_code == 404:
                        continue
                    resp.raise_for_status()
                    total = 0
                    with open(dest, "wb") as f:
                        for chunk in resp.iter_bytes(8192):
                            if chunk:
                                f.write(chunk)
                                total += len(chunk)
                                if total > max_bytes:
                                    break
                break
            except Exception:
                continue
        time.sleep(jitter(0.35, 1.0))

def smart_out_dir(base: str, query: str) -> pathlib.Path:
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    safe_q = re.sub(r"[^a-zA-Z0-9_.-]+", "_", query.strip())[:60] or "session"
    p = pathlib.Path(base) / f"{safe_q}_{today}"
    ensure_dir(p)
    return p

def goto_with_retries(page, url: str, attempts: int = 3):
    last = None
    for _ in range(attempts):
        try:
            page.goto(url, wait_until="load", timeout=35000)
            return
        except PWTimeout as e:
            last = e
            time.sleep(jitter(1.0, 2.0))
    if last:
        raise last

def build_search_url(q: str) -> str:
    qv = q.strip().replace(" ", "%20")
    return f"https://www.pinterest.com/search/pins/?q={qv}"

def main():
    ap = argparse.ArgumentParser(description="Practice-only Pinterest public search scraper.")
    ap.add_argument("--query", type=str, help="Search query, e.g. 'fall aesthetic'.")
    ap.add_argument("--url", type=str, help="Direct Pinterest URL to scrape (search or explore).")
    ap.add_argument("--limit", type=int, default=80, help="Max pins to collect.")
    ap.add_argument("--out", type=str, default="out", help="Output base folder.")
    ap.add_argument("--download", action="store_true", help="Download images as well.")
    ap.add_argument("--headless", action="store_true", help="Run browser headless.")
    ap.add_argument("--filter", type=str, default="", help="Comma-separated required keywords in title/alt.")
    ap.add_argument("--avoid", type=str, default="", help="Comma-separated words to exclude in title/alt.")
    ap.add_argument("--aspect", type=str, default="0.0,10.0", help="min,max aspect ratio (w/h).")
    ap.add_argument("--minsize", type=int, default=0, help="Minimum image area (w*h).")
    ap.add_argument("--prefer-originals", action="store_true", help="Try /originals/ before /736x/.")
    args = ap.parse_args()

    # ===== LAZY DEFAULTS (my use-case) =====
    no_args = not any([
        args.query, args.url, args.limit != 80, args.download, args.headless,
        args.filter, args.avoid, args.out != "out", args.minsize != 0,
        args.prefer_originals, args.aspect != "0.0,10.0"
    ])
    if no_args:
        args.query = "christmas snow"
        args.limit = 100
        args.download = True
        args.headless = True
        args.out = "out"
        args.prefer_originals = True

    query_for_naming = args.query or "session"
    out_dir = smart_out_dir(args.out, query_for_naming)

    target_url = args.url or build_search_url(args.query)
    ua = random.choice(UA_POOL)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=args.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        ctx = browser.new_context(
            user_agent=ua,
            viewport={"width": 1280, "height": 1000},
            locale="en-US",
            device_scale_factor=1.0,
        )
        page = ctx.new_page()

        # Stealth: reduce obvious automation signals
        page.add_init_script("""() => {
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        }""")

        page.set_default_timeout(30000)
        goto_with_retries(page, target_url)
        time.sleep(jitter(1.2, 2.2))
        best_effort_close_overlays(page)
        wait_for_feed(page)

        # Dismiss things that re-open as we scroll
        for _ in range(2):
            best_effort_close_overlays(page)

        req_keywords = tuple([s.strip() for s in (args.filter or "").split(",") if s.strip()])
        avoid_words = tuple([s.strip().lower() for s in (args.avoid or "").split(",") if s.strip()])
        a_min, a_max = (float(x) for x in args.aspect.split(","))

        rows = scroll_and_collect(
            page, args.limit,
            require_keywords=req_keywords,
            avoid_words=avoid_words,
            aspect_min=a_min, aspect_max=a_max,
            min_area=int(args.minsize),
            prefer_jpeg=False
        )

        # Save metadata
        df = pd.DataFrame(rows)
        csv_path = out_dir / "pins.csv"
        json_path = out_dir / "pins.json"
        df.to_csv(csv_path, index=False)
        df.to_json(json_path, orient="records", indent=2, force_ascii=False)

        if args.download and rows:
            download_images(rows, out_dir / "images", prefer_originals=args.prefer_originals)

        print(f"Collected {len(rows)} pins")
        print(f"CSV: {csv_path}")
        print(f"JSON: {json_path}")
        if args.download:
            print(f"Images: {(out_dir / 'images').resolve()}")

        browser.close()

if __name__ == "__main__":
    main()
