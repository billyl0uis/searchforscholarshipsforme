"""
crawler.py — Focused crawler for craft school scholarship pages.

Hard limits enforced in code (not config):
  - 20 pages per site max
  - 90 second per-site timeout via asyncio.wait_for()
  - No query strings
  - URL allowlist: only follow links containing scholarship-relevant words
  - 45 minute global job timeout
"""

import asyncio
import io
import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
import pdfplumber
from bs4 import BeautifulSoup

# ── Hard limits ────────────────────────────────────────────────────────────────
PAGE_CAP = 20           # pages per site
SITE_TIMEOUT = 90       # seconds per site
GLOBAL_TIMEOUT = 45 * 60  # 45 minutes total

# ── Keyword allowlist — only follow links containing at least one of these ─────
FOLLOW_KEYWORDS = {
    "scholarship", "grant", "award", "fellow", "assistantship",
    "residency", "residencies", "financial", "tuition", "apply",
    "fund", "funded", "stipend", "emerging", "opportunity",
}

# ── Keywords that flag a page as containing opportunity content ────────────────
CONTENT_KEYWORDS = {
    "scholarship", "grant", "award", "fellowship", "assistantship",
    "work-study", "stipend", "funded", "tuition waiver", "emerging artist",
}

# ── Priority paths to probe on every domain before general crawling ────────────
PRIORITY_PATHS = [
    "/scholarships", "/scholarship", "/financial-aid", "/financial_aid",
    "/assistantships", "/residencies", "/residency", "/fellowships",
    "/fellowship", "/apply", "/awards", "/grants", "/funding",
    "/opportunities", "/tuition", "/support",
]

# ── Path prefixes that are never scholarship pages ─────────────────────────────
SKIP_PATH_PREFIXES = (
    "/shop", "/store", "/product", "/event", "/calendar",
    "/blog", "/news", "/gallery", "/archive", "/staff",
    "/faculty", "/people", "/tag", "/category", "/feed",
    "/cdn", "/wp-json", "/wp-content", "/wp-includes",
    "/donate", "/give", "/ticket", "/cart", "/checkout",
    "/embed", "/trackback", "/author",
)

SKIP_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".avi",
    ".mov", ".webm", ".zip", ".tar", ".gz", ".exe", ".css",
    ".js", ".map",
}


def _should_skip(url: str) -> bool:
    parsed = urlparse(url)
    # No query strings — they generate infinite variations
    if parsed.query:
        return True
    path = parsed.path.lower()
    if any(path.endswith(ext) for ext in SKIP_EXTENSIONS):
        return True
    if any(path.startswith(pfx) or f"/{pfx.lstrip('/')}/" in path
           for pfx in SKIP_PATH_PREFIXES):
        return True
    return False


def _is_internal(base_url: str, url: str) -> bool:
    base = urlparse(base_url).netloc.lower()
    link = urlparse(url).netloc.lower()
    return link == base or link.endswith("." + base) or base.endswith("." + link)


def _is_allowlisted(url: str, anchor: str) -> bool:
    """Only follow links where URL path or anchor text contains a follow keyword."""
    path = urlparse(url).path.lower()
    text = (path + " " + anchor.lower())
    return any(kw in text for kw in FOLLOW_KEYWORDS)


def _normalize(url: str) -> str:
    return urlparse(url)._replace(fragment="").geturl()


def _contains_content_keywords(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in CONTENT_KEYWORDS)


def _extract_links(base_url: str, html: str) -> list[tuple[str, str]]:
    """Return (url, anchor_text) pairs that are internal, clean, and allowlisted."""
    soup = BeautifulSoup(html, "lxml")
    seen = set()
    results = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        full = _normalize(urljoin(base_url, href))
        parsed = urlparse(full)
        if parsed.scheme not in ("http", "https"):
            continue
        if full in seen:
            continue
        seen.add(full)
        if not _is_internal(base_url, full):
            continue
        if _should_skip(full):
            continue
        anchor = tag.get_text(strip=True)
        if not _is_allowlisted(full, anchor):
            continue
        results.append((full, anchor))
    return results


def _extract_pdf_links(base_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    return list({
        _normalize(urljoin(base_url, tag["href"]))
        for tag in soup.find_all("a", href=True)
        if tag["href"].strip().lower().endswith(".pdf")
    })


async def _head_ok(url: str, client: httpx.AsyncClient) -> bool:
    try:
        r = await client.head(url, timeout=5, follow_redirects=True)
        return r.status_code < 400
    except Exception:
        return False


async def _fetch_html(url: str, client: httpx.AsyncClient) -> Optional[str]:
    try:
        r = await client.get(url, timeout=15, follow_redirects=True)
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if "html" in ct or not ct:
            return r.text
    except Exception as e:
        print(f"    [fetch error] {e}")
    return None


async def _fetch_html_playwright(url: str) -> Optional[str]:
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, timeout=20000, wait_until="domcontentloaded")
            await asyncio.sleep(1)
            html = await page.content()
            await browser.close()
            return html
    except Exception as e:
        print(f"    [playwright error] {e}")
        return None


def _fetch_pdf(url: str) -> Optional[str]:
    try:
        r = httpx.get(url, timeout=15, follow_redirects=True)
        r.raise_for_status()
        parts = []
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            for pg in pdf.pages[:10]:
                t = pg.extract_text()
                if t:
                    parts.append(t)
        return "\n".join(parts)
    except Exception as e:
        print(f"    [pdf error] {e}")
        return None


def _page_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


async def _crawl_site_inner(base_url: str, depth: int) -> list[dict]:
    """Core crawl logic — called inside asyncio.wait_for()."""
    school = urlparse(base_url).netloc.replace("www.", "")
    visited: set[str] = set()
    results: list[dict] = []
    # queue: (url, current_depth)
    queue: list[tuple[str, int]] = []

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; ScholarshipBot/1.0)"},
        follow_redirects=True,
    ) as client:
        # Phase 1: probe priority paths
        probed = []
        for path in PRIORITY_PATHS:
            url = _normalize(f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}{path}")
            if await _head_ok(url, client):
                probed.append(url)
        print(f"  Priority paths found: {len(probed)}/{len(PRIORITY_PATHS)}")

        # Homepage first, then priority hits, then general
        queue = [(base_url, 0)] + [(u, 1) for u in probed]

        while queue and len(visited) < PAGE_CAP:
            url, d = queue.pop(0)
            url = _normalize(url)
            if url in visited or _should_skip(url):
                continue
            visited.add(url)

            n = len(visited)
            print(f"  [{n}/{PAGE_CAP}] {url}")

            # PDF
            if url.lower().endswith(".pdf"):
                text = _fetch_pdf(url)
                if text and _contains_content_keywords(text):
                    results.append({"url": url, "html_text": text, "page_type": "pdf", "school": school})
                    print(f"    ✓ pdf flagged")
                continue

            html = await _fetch_html(url, client)
            if html is None or len(html) < 300:
                html = await _fetch_html_playwright(url)
            if not html:
                continue

            text = _page_text(html)
            if _contains_content_keywords(text):
                results.append({"url": url, "html_text": text[:40000], "page_type": "page", "school": school})
                print(f"    ✓ flagged")

            if d < depth and len(visited) < PAGE_CAP:
                for link_url, anchor in _extract_links(base_url, html):
                    if link_url not in visited:
                        queue.append((link_url, d + 1))
                for pdf_url in _extract_pdf_links(base_url, html):
                    if pdf_url not in visited:
                        queue.append((pdf_url, d + 1))

    print(f"  Done: {len(visited)} pages visited, {len(results)} flagged")
    return results


async def crawl_site(base_url: str, depth: int = 3) -> list[dict]:
    """Crawl one site with a hard 90-second timeout."""
    try:
        return await asyncio.wait_for(
            _crawl_site_inner(base_url, depth),
            timeout=SITE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        print(f"  [TIMEOUT] {base_url} hit {SITE_TIMEOUT}s limit — moving on")
        return []
    except Exception as e:
        print(f"  [ERROR] {base_url}: {e}")
        return []


async def crawl_all_sites(
    targets: list[str],
    max_depth: int = 3,
    **_kwargs,  # absorb unused config keys (keyword_flags, timeouts, etc.)
) -> dict[str, list[dict]]:
    """Crawl all sites with a 45-minute global timeout."""
    results = {}
    global_start = time.monotonic()

    for i, url in enumerate(targets, 1):
        elapsed = time.monotonic() - global_start
        if elapsed >= GLOBAL_TIMEOUT:
            print(f"\n[GLOBAL TIMEOUT] 45-minute limit reached after {i-1}/{len(targets)} sites")
            break
        print(f"\n[{i}/{len(targets)}] {url}  (elapsed: {elapsed/60:.1f}m)")
        results[url] = await crawl_site(url, depth=max_depth)

    total = time.monotonic() - global_start
    print(f"\nCrawl complete: {len(results)}/{len(targets)} sites in {total/60:.1f}m")
    return results
