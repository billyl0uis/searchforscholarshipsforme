"""
crawler.py — httpx-only crawler for craft school scholarship pages.

No Playwright. Strict timeouts enforced at every level.
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
PAGE_CAP = 15
SITE_TIMEOUT = 60        # seconds — enforced via asyncio.wait_for()
GLOBAL_TIMEOUT = 45 * 60 # 45 minutes total
REQUEST_TIMEOUT = 10.0   # seconds per HTTP request

# ── Only follow links whose URL path or anchor text contains one of these ──────
FOLLOW_KEYWORDS = {
    "scholarship", "grant", "award", "fellow", "assistantship",
    "residency", "residencies", "financial", "tuition", "apply",
    "fund", "funded", "stipend", "emerging", "opportunity",
}

# ── Page is flagged if its text contains one of these ─────────────────────────
CONTENT_KEYWORDS = {
    "scholarship", "grant", "award", "fellowship", "assistantship",
    "work-study", "stipend", "funded", "tuition waiver", "emerging artist",
}

# ── Paths to probe first on every domain ──────────────────────────────────────
PRIORITY_PATHS = [
    "/scholarships", "/scholarship", "/financial-aid", "/financial_aid",
    "/assistantships", "/residencies", "/residency", "/fellowships",
    "/fellowship", "/apply", "/awards", "/grants", "/funding",
    "/opportunities", "/tuition", "/support",
]

# ── Path segments that are never scholarship pages ─────────────────────────────
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
    text = urlparse(url).path.lower() + " " + anchor.lower()
    return any(kw in text for kw in FOLLOW_KEYWORDS)


def _normalize(url: str) -> str:
    return urlparse(url)._replace(fragment="").geturl()


def _contains_content_keywords(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in CONTENT_KEYWORDS)


def _page_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


def _extract_links(base_url: str, html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    results = []
    for tag in soup.find_all("a", href=True):
        full = _normalize(urljoin(base_url, tag["href"].strip()))
        if full in seen:
            continue
        seen.add(full)
        if urlparse(full).scheme not in ("http", "https"):
            continue
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


async def _get(url: str, client: httpx.AsyncClient) -> Optional[str]:
    """Fetch a URL. Returns HTML text or None. Never raises, never retries."""
    try:
        r = await client.get(url, follow_redirects=True)
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if "html" in ct or not ct:
            return r.text
        return None
    except httpx.TimeoutException:
        print(f"    [SKIP] {url} — timeout after {REQUEST_TIMEOUT}s")
        return None
    except httpx.HTTPStatusError as e:
        print(f"    [SKIP] {url} — HTTP {e.response.status_code}")
        return None
    except httpx.ConnectError as e:
        print(f"    [SKIP] {url} — connection/SSL error: {e}")
        return None
    except Exception as e:
        print(f"    [SKIP] {url} — {type(e).__name__}: {e}")
        return None


async def _get_pdf(url: str, client: httpx.AsyncClient) -> Optional[str]:
    """Fetch and parse a PDF. Returns extracted text or None. Never raises."""
    try:
        r = await client.get(url, follow_redirects=True)
        r.raise_for_status()
        content = r.content

        def _parse():
            parts = []
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                for pg in pdf.pages[:10]:
                    t = pg.extract_text()
                    if t:
                        parts.append(t)
            return "\n".join(parts)

        # 20s cap on PDF parsing — pdfplumber can hang on malformed PDFs
        return await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _parse),
            timeout=20.0,
        )
    except asyncio.TimeoutError:
        print(f"    [SKIP] {url} — pdf parse timeout (20s)")
        return None
    except httpx.TimeoutException:
        print(f"    [SKIP] {url} — pdf download timeout")
        return None
    except Exception as e:
        print(f"    [SKIP] {url} — pdf {type(e).__name__}: {e}")
        return None


async def _crawl_site_inner(base_url: str, depth: int) -> list[dict]:
    school = urlparse(base_url).netloc.replace("www.", "")
    visited: set[str] = set()
    results: list[dict] = []

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ScholarshipBot/1.0)"},
    ) as client:
        # Probe priority paths concurrently
        origin = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"
        probe_urls = [_normalize(origin + p) for p in PRIORITY_PATHS]

        async def _probe(u):
            try:
                r = await client.head(u, follow_redirects=True)
                return u if r.status_code < 400 else None
            except Exception:
                return None

        probe_hits = await asyncio.gather(*[_probe(u) for u in probe_urls])
        probed = [u for u in probe_hits if u]
        print(f"  Priority paths found: {len(probed)}/{len(PRIORITY_PATHS)}")

        queue: list[tuple[str, int]] = [(base_url, 0)] + [(u, 1) for u in probed]

        while queue and len(visited) < PAGE_CAP:
            url, d = queue.pop(0)
            url = _normalize(url)
            if url in visited or _should_skip(url):
                continue
            visited.add(url)

            print(f"  [{len(visited)}/{PAGE_CAP}] {url}", flush=True)

            if url.lower().endswith(".pdf"):
                print(f"    [PDF] fetching...", flush=True)
                text = await _get_pdf(url, client)
                if text and _contains_content_keywords(text):
                    results.append({"url": url, "html_text": text, "page_type": "pdf", "school": school})
                    print(f"    ✓ pdf flagged")
                continue

            html = await _get(url, client)
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

    print(f"  Done: {len(visited)} pages, {len(results)} flagged")
    return results


async def crawl_site(base_url: str, depth: int = 3) -> list[dict]:
    """Crawl one site. Hard 60-second ceiling via asyncio.wait_for()."""
    try:
        return await asyncio.wait_for(
            _crawl_site_inner(base_url, depth),
            timeout=SITE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        print(f"  [TIMEOUT] {base_url} — {SITE_TIMEOUT}s site limit reached, moving on")
        return []
    except Exception as e:
        print(f"  [ERROR] {base_url}: {e}")
        return []


async def crawl_all_sites(
    targets: list[str],
    max_depth: int = 3,
    **_kwargs,
) -> dict[str, list[dict]]:
    """Crawl all sites. Hard 45-minute global ceiling."""
    results = {}
    global_start = time.monotonic()

    for i, url in enumerate(targets, 1):
        elapsed = time.monotonic() - global_start
        if elapsed >= GLOBAL_TIMEOUT:
            print(f"\n[GLOBAL TIMEOUT] 45-minute limit after {i-1}/{len(targets)} sites")
            break
        print(f"\n[{i}/{len(targets)}] {url}  (elapsed: {elapsed/60:.1f}m)")
        results[url] = await crawl_site(url, depth=max_depth)

    total = time.monotonic() - global_start
    print(f"\nCrawl complete: {len(results)}/{len(targets)} sites in {total/60:.1f}m")
    return results
