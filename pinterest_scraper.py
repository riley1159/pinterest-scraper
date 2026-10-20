#!/usr/bin/env python3
"""Collect image references from public Pinterest pages and optionally download them."""

import argparse
import csv
import datetime as dt
import hashlib
import json
import logging
import math
from pathlib import Path
import random
import re
import tempfile
import time
from urllib.parse import urlencode, urljoin, urlsplit
import warnings

import httpx
from PIL import Image, UnidentifiedImageError
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

LOG = logging.getLogger(__name__)
PIN_HOST = "https://www.pinterest.com"
FIELDS = (
    "pin_id", "pin_url", "image_url", "title", "alt", "img_w", "img_h",
    "download_status", "download_url", "local_path", "download_w", "download_h",
    "download_bytes", "download_error",
)
IMAGE_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}
EXTRACT_PINS = """() => Array.from(document.querySelectorAll('a[href*="/pin/"]'))
    .flatMap(a => {
        const img = a.querySelector('img');
        if (!img) return [];
        const alt = img.alt || a.getAttribute('aria-label') || '';
        return [{href: a.href, src: img.currentSrc || img.src || '',
                 alt, title: alt, w: img.naturalWidth, h: img.naturalHeight}];
    })"""


def is_pinterest_url(value):
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    return (parsed.scheme == "https" and not parsed.username and not parsed.password
            and (host == "pinterest.com" or host.endswith(".pinterest.com")))


def is_image_url(value):
    parsed = urlsplit(value)
    return (parsed.scheme == "https" and parsed.hostname == "i.pinimg.com"
            and not parsed.username and not parsed.password)


def extract_pin_id(href):
    match = re.fullmatch(r"/pin/(\d+)/?", urlsplit(href).path)
    return match.group(1) if match else ""


def build_search_url(query):
    return f"{PIN_HOST}/search/pins/?{urlencode({'q': query.strip()})}"


def upgraded_url_candidates(url, prefer_originals=True):
    """Request existing CDN variants; this does not upscale image pixels."""
    candidates = []
    if prefer_originals:
        candidates.append(re.sub(r"/\d+x/", "/originals/", url))
    candidates.extend([re.sub(r"/\d+x/", "/736x/", url), url])
    return list(dict.fromkeys(candidates))


def scroll_and_collect(page, limit, require_keywords=(), avoid_words=(),
                       aspect_min=0.0, aspect_max=math.inf, min_area=0):
    """Collect unique, loaded pins, stopping after ten passes without additions."""
    seen = set()
    items = []
    idle = 0
    while len(items) < limit and idle < 10:
        added = 0
        for row in page.evaluate(EXTRACT_PINS):
            href = urljoin(PIN_HOST, row.get("href", ""))
            image_url = row.get("src", "")
            pin_id = extract_pin_id(href)
            if not is_pinterest_url(href) or not pin_id or not is_image_url(image_url):
                continue
            if pin_id in seen:
                continue
            width, height = int(row.get("w") or 0), int(row.get("h") or 0)
            # A lazy-loaded image must be reconsidered on subsequent passes.
            if width <= 0 or height <= 0:
                continue
            text = f"{row.get('title', '')} {row.get('alt', '')}".casefold()
            if any(word.casefold() in text for word in avoid_words):
                continue
            if require_keywords and not any(word.casefold() in text for word in require_keywords):
                continue
            if width * height < min_area or not aspect_min <= width / height <= aspect_max:
                continue
            seen.add(pin_id)
            items.append({
                "pin_id": pin_id, "pin_url": f"{PIN_HOST}/pin/{pin_id}/",
                "image_url": image_url, "title": (row.get("title") or "").strip(),
                "alt": (row.get("alt") or "").strip(), "img_w": width, "img_h": height,
                "download_status": "not_requested",
            })
            added += 1
            if len(items) == limit:
                break
        idle = 0 if added else idle + 1
        if len(items) >= limit or idle >= 10:
            break
        page.mouse.wheel(0, 2000)
        time.sleep(random.uniform(1.5, 3.0))
    items.sort(key=lambda item: item["img_w"] * item["img_h"], reverse=True)
    return items


def inspect_image(path):
    """Verify the container and decode the image before accepting a download."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as image:
            extension = IMAGE_EXTENSIONS.get(image.format)
            if extension is None:
                raise ValueError(f"Unsupported image format: {image.format}")
            image.verify()
        with Image.open(path) as image:
            image.load()
            return extension, image.width, image.height


def download_images(rows, folder, max_bytes=25_000_000, prefer_originals=True):
    """Save only complete, decodable images; retain each result in its record."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=40.0, follow_redirects=False,
                      headers={"Accept-Encoding": "identity"}) as client:
        for row in rows:
            row.update(download_status="failed", download_error="No image candidate succeeded")
            for url in upgraded_url_candidates(row["image_url"], prefer_originals):
                temporary = None
                try:
                    if not is_image_url(url):
                        raise ValueError("Image URL must use https://i.pinimg.com")
                    with client.stream("GET", url) as response:
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "").split(";")[0].lower()
                        if not content_type.startswith("image/"):
                            raise ValueError(f"Expected image content, received {content_type or 'no content type'}")
                        length = response.headers.get("content-length")
                        if length and int(length) > max_bytes:
                            raise ValueError(f"Image exceeds {max_bytes} bytes")
                        total = 0
                        with tempfile.NamedTemporaryFile(dir=folder, suffix=".part", delete=False) as output:
                            temporary = Path(output.name)
                            for chunk in response.iter_bytes(8192):
                                total += len(chunk)
                                if total > max_bytes:
                                    raise ValueError(f"Image exceeds {max_bytes} bytes")
                                output.write(chunk)
                        if length and not response.headers.get("content-encoding") and total != int(length):
                            raise ValueError("Incomplete response body")
                    extension, width, height = inspect_image(temporary)
                    pin_id = str(row.get("pin_id", ""))
                    name = pin_id if pin_id.isascii() and pin_id.isdigit() else hashlib.sha256(url.encode()).hexdigest()
                    destination = folder / f"{name}{extension}"
                    temporary.replace(destination)
                    row.update(download_status="downloaded", download_url=url,
                               local_path=f"{folder.name}/{destination.name}", download_w=width,
                               download_h=height, download_bytes=total, download_error="")
                    break
                except (httpx.HTTPError, OSError, ValueError, SyntaxError,
                        UnidentifiedImageError, Image.DecompressionBombError,
                        Image.DecompressionBombWarning) as error:
                    row["download_error"] = f"{type(error).__name__}: {error}"
                    LOG.debug("Candidate failed: %s (%s)", url, error)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            if row["download_status"] == "failed":
                LOG.warning("Download failed for pin %s: %s", row["pin_id"], row["download_error"])
            time.sleep(random.uniform(0.35, 1.0))
    return {status: sum(row["download_status"] == status for row in rows)
            for status in ("downloaded", "failed")}


def smart_out_dir(base, query):
    """Allocate a unique run directory without overwriting previous exports."""
    label = re.sub(r"[^a-zA-Z0-9_.-]+", "_", query.strip()).strip("._-")[:60] or "session"
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return Path(tempfile.mkdtemp(prefix=f"{label}_{stamp}_", dir=base))


def goto_with_retries(page, url, attempts=3):
    for attempt in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=35000)
            return
        except PlaywrightTimeout:
            if attempt == attempts - 1:
                raise
            LOG.warning("Navigation timed out; retrying (%s/%s)", attempt + 1, attempts)
            time.sleep(1)


def save_metadata(rows, folder):
    """Keep a stable CSV schema even when no pins were collected."""
    (folder / "pins.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with (folder / "pins.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def aspect_range(value):
    try:
        lower, upper = map(float, value.split(","))
        if not (math.isfinite(lower) and math.isfinite(upper) and 0 <= lower < upper):
            raise ValueError
        return lower, upper
    except ValueError as error:
        raise argparse.ArgumentTypeError("use finite min,max values with 0 <= min < max") from error


def nonempty(value):
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be blank")
    return value.strip()


def pinterest_url(value):
    try:
        if is_pinterest_url(value):
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("use an HTTPS pinterest.com URL")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--query", type=nonempty, help="Pinterest search text")
    target.add_argument("--url", type=pinterest_url, help="Public HTTPS pinterest.com page")
    parser.add_argument("--limit", type=positive_int, default=80, help="Maximum pins (default: 80)")
    parser.add_argument("--out", default="out", help="Output base directory (default: out)")
    parser.add_argument("--download", action="store_true", help="Download collected images")
    parser.add_argument("--headless", action="store_true", help="Run without a browser window")
    parser.add_argument("--filter", default="", help="Comma-separated substrings; match ANY (case insensitive)")
    parser.add_argument("--avoid", default="", help="Comma-separated excluded substrings; no exclusions by default")
    parser.add_argument("--aspect", type=aspect_range, default=(0.0, math.inf), help="Observed width/height range: min,max")
    parser.add_argument("--minsize", type=nonnegative_int, default=0, help="Minimum observed image area in pixels")
    parser.add_argument("--prefer-originals", action="store_true", help="Try original-resolution URLs before 736px variants")
    parser.add_argument("--verbose", action="store_true", help="Show diagnostic logs")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")
    target = args.url or build_search_url(args.query)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=args.headless, chromium_sandbox=True)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 1000}, locale="en-US")
                page = context.new_page()
                goto_with_retries(page, target)
                page.wait_for_selector('a[href*="/pin/"] img', timeout=20000)
                rows = scroll_and_collect(
                    page, args.limit,
                    require_keywords=tuple(word.strip() for word in args.filter.split(",") if word.strip()),
                    avoid_words=tuple(word.strip() for word in args.avoid.split(",") if word.strip()),
                    aspect_min=args.aspect[0], aspect_max=args.aspect[1], min_area=args.minsize,
                )
            finally:
                browser.close()
        folder = smart_out_dir(args.out, args.query or "url_session")
        save_metadata(rows, folder)
        summary = {"downloaded": 0, "failed": 0}
        if args.download and rows:
            for row in rows:
                row["download_status"] = "pending"
            try:
                summary = download_images(rows, folder / "images", prefer_originals=args.prefer_originals)
            finally:
                save_metadata(rows, folder)
        run = {
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "target_url": target, "options": vars(args), "collected": len(rows), **summary,
        }
        # JSON has no representation for infinity; an omitted upper bound is null.
        run["options"]["aspect"] = [value if math.isfinite(value) else None for value in args.aspect]
        (folder / "run.json").write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        LOG.info("Collected %s/%s pins. Output: %s", len(rows), args.limit, folder.resolve())
        if len(rows) < args.limit:
            LOG.warning("Collection stopped below the limit; filters, unavailable results, or a login prompt may be responsible")
        if args.download:
            LOG.info("Downloads: %s succeeded, %s failed", summary["downloaded"], summary["failed"])
        return 1 if not rows or summary["failed"] else 0
    except (PlaywrightError, httpx.HTTPError, OSError) as error:
        LOG.error("Run failed: %s", error)
        return 1
    except KeyboardInterrupt:
        LOG.warning("Interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
