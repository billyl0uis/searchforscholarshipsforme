"""
crawler.py — Recursive web crawler for craft school scholarship pages.

Uses Playwright for JS-rendered pages and httpx+lxml for static pages.
Parses linked PDFs with pdfplumber.
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

KEYWORD_FLAGS = [
    "scholarship",
    "grant",
    "award",
    "fellowship",
    "assistantship",
    "work-study",
    "stipend",
    "funded",
    "tuition waiver",
    "emerging artist",
]

# Paths to probe first on every domain before general crawling
PRIORITY_PATHS = [
    "/scholarships",
    "/scholarship",
    "/financial-aid",
    "/financial_aid",
    "/assistantships",
    "/assistantship",
    "/residencies",
    "/residency",
    "/fellowships",
    "/fellowship",
    "/apply",
    "/awards",
    "/award",
    "/grants",
    "/grant",
    "/programs",
    "/tuition",
    "/support",
    "/funding",
    "/opportunities",
]

# URL path segments that signal high-value pages worth following
RELEVANT_PATH_KEYWORDS = {
    "scholarship", "financial", "aid", "assistantship", "residenc",
    "fellowship", "award", "grant", "stipend", "fund", "opport",
    "apply", "admission", "program", "tuition", "support", "emerging",
    "artist", "craft", "glass", "metal", "jewelry", "studio", "news",
    "announc",
}

# Anchor text words that signal a link is worth following
RELEVANT_ANCHOR_KEYWORDS = {
    "scholarship", "financial aid", "assistantship", "residency",
    "fellowship", "award", "grant", "stipend", "funding", "apply",
    "opportunity", "program", "tuition", "support",
}

# Subdomain prefixes that will never have scholarship content — skip entirely
SKIP_SUBDOMAINS = {
    "store", "shop", "tickets", "give", "donate", "cart", "checkout",
    "mail", "email", "cdn", "static", "assets", "media", "images",
    "api", "dev", "staging", "sandbox", "test",
}

# URL path segments to skip — none of these ever contain scholarship info
SKIP_PATH_SEGMENTS = {
    "/cdn-cgi/", "/wp-json/", "/feed/", "/tag/", "/page/",
    "/wp-content/uploads/", "/wp-includes/", "/xmlrpc",
    "/trackback/", "/embed/", "/oembed/", "/cart/", "/checkout/",
    "/store/", "/shop/", "/product/", "/author/", "/staff/",
    "/faculty/", "/people/", "/calendar/", "/events/", "/event/",
    "/ticket/", "/donate/", "/give/", "/blog/", "/news/",
    "/gallery/", "/archive/", "/resources/",
}

SKIP_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".woff", ".woff2", ".ttf", ".eot",
    ".mp4", ".mp3", ".avi", ".mov", ".webm",
    ".zip", ".tar", ".gz", ".exe", ".dmg",
    ".css", ".js", ".map",
}

PER_SITE_TIMEOUT = 2 * 60       # 2 minutes per domain
PER_SITE_PAGE_CAP = 30          # max pages per domain
GLOBAL_TIMEOUT = 4 * 60 * 60   # 4 hours total


def _contains_keywords(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in KEYWORD_FLAGS)


def _is_relevant_link(url: str, anchor_text: str = "") -> bool:
    """Return True if a link looks worth following based on URL path or anchor text."""
    path = urlparse(url).path.lower()
    anchor = anchor_text.lower()

    # Always follow PDF links
    if path.endswith(".pdf"):
        return True

    # Follow if the path contains any relevant keyword
    for kw in RELEVANT_PATH_KEYWORDS:
        if kw in path:
            return True

    # Follow if anchor text contains any relevant phrase
    for kw in RELEVANT_ANCHOR_KEYWORDS:
        if kw in anchor:
            return True

    # Skip likely junk: blog-style date paths (/2024/03/event-recap)
    if re.search(r"/\d{4}/\d{2}/", path):
        return False

    return False


def _should_skip_subdomain(base_url: str, link_url: str) -> bool:
    """Skip subdomains like store.*, shop.*, tickets.*, give.* etc."""
    base_netloc = urlparse(base_url).netloc.lower().lstrip("www.")
    link_parsed = urlparse(link_url)
    link_netloc = link_parsed.netloc.lower()

    # Extract subdomain portion
    if link_netloc.endswith(base_netloc):
        subdomain = link_netloc[: -(len(base_netloc))].rstrip(".")
        if subdomain and subdomain.split(".")[-1] in SKIP_SUBDOMAINS:
            return True
    return False


def _should_skip_url(url: str) -> bool:
    """Return True for URLs that are definitely not scholarship pages."""
    parsed = urlparse(url)
    path_lower = parsed.path.lower()

    # Skip any URL with a query string — ?type= / ?focus= etc. generate
    # infinite variations and cause the crawler to loop forever
    if parsed.query:
        return True

    for ext in SKIP_EXTENSIONS:
        if path_lower.endswith(ext):
            return True

    for seg in SKIP_PATH_SEGMENTS:
        if seg in path_lower:
            return True

    return False


def _is_internal(base_url: str, link: str) -> bool:
    base_netloc = urlparse(base_url).netloc.lower()
    link_netloc = urlparse(link).netloc.lower()
    return (
        link_netloc == base_netloc
        or link_netloc.endswith("." + base_netloc)
        or base_netloc.endswith("." + link_netloc)
    )


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(fragment="").geturl()


def _extract_links(base_url: str, html: str, internal_only: bool = True) -> list[tuple[str, str]]:
    """Return list of (url, anchor_text) tuples."""
    soup = BeautifulSoup(html, "lxml")
    links = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        anchor = tag.get_text(strip=True)
        full = urljoin(base_url, href)
        full = _normalize_url(full)
        parsed = urlparse(full)
        if parsed.scheme not in ("http", "https"):
            continue
        if internal_only and not _is_internal(base_url, full):
            continue
        if _should_skip_url(full):
            continue
        if _should_skip_subdomain(base_url, full):
            continue
        links.append((full, anchor))
    # Deduplicate by URL
    seen = set()
    result = []
    for url, anchor in links:
        if url not in seen:
            seen.add(url)
            result.append((url, anchor))
    return result


def _extract_pdf_links(base_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    pdfs = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        full = urljoin(base_url, href)
        if full.lower().endswith(".pdf"):
            pdfs.append(full)
    return list(set(pdfs))


def _classify_page(url: str, text: str) -> str:
    url_lower = url.lower()
    text_lower = text.lower()
    if "scholarship" in url_lower or "scholarship" in text_lower[:500]:
        return "scholarship"
    if "residenc" in url_lower or "residency" in text_lower[:500]:
        return "residency"
    if "fellowship" in url_lower or "fellowship" in text_lower[:500]:
        return "fellowship"
    if "financial" in url_lower or "financial aid" in text_lower[:500]:
        return "financial_aid"
    if "apply" in url_lower or "admission" in url_lower:
        return "admissions"
    if "news" in url_lower or "announc" in url_lower:
        return "news"
    if ".pdf" in url_lower:
        return "pdf"
    return "general"


async def _fetch_with_playwright(url: str) -> Optional[str]:
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            html = await page.content()
            await browser.close()
            return html
    except Exception as e:
        print(f"    [Playwright error] {url}: {e}")
        return None


async def _fetch_static(url: str, client: httpx.AsyncClient) -> Optional[str]:
    try:
        resp = await client.get(url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "html" in ct or ct == "":
            return resp.text
        return None
    except Exception as e:
        print(f"    [httpx error] {url}: {e}")
        return None


async def _head_exists(url: str, client: httpx.AsyncClient) -> bool:
    """Quick HEAD check — returns False if 404 or error."""
    try:
        resp = await client.head(url, timeout=8, follow_redirects=True)
        return resp.status_code < 400
    except Exception:
        return False


def _fetch_pdf_text(url: str) -> Optional[str]:
    try:
        resp = httpx.get(url, timeout=20, follow_redirects=True)
        resp.raise_for_status()
        buf = io.BytesIO(resp.content)
        text_parts = []
        with pdfplumber.open(buf) as pdf:
            for page in pdf.pages[:20]:
                t = page.extract_text()
                if t:
                    text_parts.append(t)
        return "\n".join(text_parts)
    except Exception as e:
        print(f"    [PDF error] {url}: {e}")
        return None


async def crawl_site(
    base_url: str,
    max_depth: int = 3,
    internal_links_only: bool = True,
    keyword_flags: Optional[list] = None,
    site_timeout: int = PER_SITE_TIMEOUT,
    page_cap: int = PER_SITE_PAGE_CAP,
) -> list[dict]:
    """
    Crawl a site with priority-path probing, per-site timeout, and page cap.
    Returns list of dicts: {url, html_text, page_type, school}
    """
    if keyword_flags:
        global KEYWORD_FLAGS
        KEYWORD_FLAGS = keyword_flags

    parsed_base = urlparse(base_url)
    base_origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
    school = parsed_base.netloc.replace("www.", "")
    visited: set[str] = set()
    results: list[dict] = []
    site_start = time.monotonic()

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; ScholarshipBot/1.0)"},
        follow_redirects=True,
    ) as client:

        # ── Phase 1: probe priority paths ──────────────────────────
        priority_queue: list[tuple[str, int]] = []
        print(f"  Probing {len(PRIORITY_PATHS)} priority paths...")
        for path in PRIORITY_PATHS:
            probe_url = _normalize_url(base_origin + path)
            if await _head_exists(probe_url, client):
                priority_queue.append((probe_url, 1))

        # The homepage always goes first
        queue: list[tuple[str, int]] = [(base_url, 0)] + priority_queue

        # ── Phase 2: general crawl ──────────────────────────────────
        while queue:
            elapsed = time.monotonic() - site_start
            if elapsed >= site_timeout:
                print(f"  [TIMEOUT] {school}: {site_timeout}s limit after {len(visited)} pages")
                break
            if len(visited) >= page_cap:
                print(f"  [CAP] {school}: {page_cap}-page limit reached")
                break

            url, depth = queue.pop(0)
            url = _normalize_url(url)

            if url in visited:
                continue
            if _should_skip_url(url):
                continue
            if _should_skip_subdomain(base_url, url):
                continue

            visited.add(url)

            if depth > max_depth:
                continue

            page_num = len(visited)
            print(f"  [{page_num}/{page_cap}] depth={depth} {url}")

            is_pdf = url.lower().endswith(".pdf")
            if is_pdf:
                pdf_text = _fetch_pdf_text(url)
                if pdf_text and _contains_keywords(pdf_text):
                    results.append({
                        "url": url,
                        "html_text": pdf_text,
                        "page_type": "pdf",
                        "school": school,
                    })
                continue

            html = await _fetch_static(url, client)
            if html is None or len(html) < 500:
                html = await _fetch_with_playwright(url)
            if not html:
                continue

            soup = BeautifulSoup(html, "lxml")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)

            if _contains_keywords(text):
                page_type = _classify_page(url, text)
                results.append({
                    "url": url,
                    "html_text": text[:50000],
                    "page_type": page_type,
                    "school": school,
                })
                print(f"    ✓ flagged ({page_type})")

            if depth < max_depth:
                all_links = _extract_links(url, html, internal_links_only)
                # Only enqueue links that look relevant (URL or anchor text)
                relevant = [
                    (lurl, anchor) for lurl, anchor in all_links
                    if lurl not in visited and _is_relevant_link(lurl, anchor)
                ]
                for link_url, _ in relevant:
                    queue.append((link_url, depth + 1))

                for pdf_url in _extract_pdf_links(url, html):
                    if pdf_url not in visited:
                        queue.append((pdf_url, depth + 1))

    elapsed = time.monotonic() - site_start
    print(f"  Done {school}: {len(visited)} pages in {elapsed:.0f}s, {len(results)} flagged")
    return results


async def crawl_all_sites(
    targets: list[str],
    max_depth: int = 3,
    internal_links_only: bool = True,
    keyword_flags: Optional[list] = None,
    site_timeout: int = PER_SITE_TIMEOUT,
    page_cap: int = PER_SITE_PAGE_CAP,
    global_timeout: int = GLOBAL_TIMEOUT,
) -> dict[str, list[dict]]:
    """Crawl all target sites with a global timeout. Returns {base_url: [pages]}."""
    all_results = {}
    global_start = time.monotonic()

    for i, url in enumerate(targets, 1):
        global_elapsed = time.monotonic() - global_start
        if global_elapsed >= global_timeout:
            print(f"\n[GLOBAL TIMEOUT] {global_timeout/3600:.1f}h limit after {i-1}/{len(targets)} sites")
            break

        remaining = global_timeout - global_elapsed
        effective_timeout = min(site_timeout, int(remaining))

        print(f"\n[{i}/{len(targets)}] {url}  (global: {global_elapsed/60:.1f}m elapsed)")
        try:
            pages = await crawl_site(
                url,
                max_depth=max_depth,
                internal_links_only=internal_links_only,
                keyword_flags=keyword_flags,
                site_timeout=effective_timeout,
                page_cap=page_cap,
            )
            all_results[url] = pages
        except Exception as e:
            print(f"  ERROR crawling {url}: {e}")
            all_results[url] = []

    total = time.monotonic() - global_start
    print(f"\nCrawl complete: {len(all_results)}/{len(targets)} sites in {total/60:.1f}m")
    return all_results
