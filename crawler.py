"""
crawler.py — Recursive web crawler for craft school scholarship pages.

Uses Playwright for JS-rendered pages and httpx+lxml for static pages.
Parses linked PDFs with pdfplumber.
"""

import asyncio
import io
import re
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

# Pages likely to contain opportunities
PRIORITY_PATH_PATTERNS = [
    r"scholarship",
    r"financial.?aid",
    r"assistantship",
    r"residenc",
    r"fellowship",
    r"news",
    r"announc",
    r"apply",
    r"admission",
    r"award",
    r"grant",
    r"stipend",
    r"opport",
    r"fund",
]


def _contains_keywords(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in KEYWORD_FLAGS)


def _is_priority_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(re.search(p, path) for p in PRIORITY_PATH_PATTERNS)


def _is_internal(base_url: str, link: str) -> bool:
    base_netloc = urlparse(base_url).netloc.lower()
    link_netloc = urlparse(link).netloc.lower()
    # Allow same domain or subdomain
    return (
        link_netloc == base_netloc
        or link_netloc.endswith("." + base_netloc)
        or base_netloc.endswith("." + link_netloc)
    )


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    # Strip fragment
    return parsed._replace(fragment="").geturl()


def _extract_links(base_url: str, html: str, internal_only: bool = True) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    links = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        full = urljoin(base_url, href)
        full = _normalize_url(full)
        parsed = urlparse(full)
        if parsed.scheme not in ("http", "https"):
            continue
        if internal_only and not _is_internal(base_url, full):
            continue
        links.append(full)
    return list(set(links))


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
        resp = await client.get(url, timeout=20, follow_redirects=True)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "html" in ct or ct == "":
            return resp.text
        return None
    except Exception as e:
        print(f"    [httpx error] {url}: {e}")
        return None


def _fetch_pdf_text(url: str) -> Optional[str]:
    try:
        resp = httpx.get(url, timeout=20, follow_redirects=True)
        resp.raise_for_status()
        buf = io.BytesIO(resp.content)
        text_parts = []
        with pdfplumber.open(buf) as pdf:
            for page in pdf.pages[:20]:  # limit pages
                t = page.extract_text()
                if t:
                    text_parts.append(t)
        return "\n".join(text_parts)
    except Exception as e:
        print(f"    [PDF error] {url}: {e}")
        return None


async def crawl_site(
    base_url: str,
    max_depth: int = 5,
    internal_links_only: bool = True,
    keyword_flags: Optional[list] = None,
) -> list[dict]:
    """
    Crawl a site recursively up to max_depth.
    Returns list of dicts: {url, html_text, page_type, school}
    """
    if keyword_flags:
        global KEYWORD_FLAGS
        KEYWORD_FLAGS = keyword_flags

    school = urlparse(base_url).netloc.replace("www.", "")
    visited: set[str] = set()
    results: list[dict] = []
    # Queue: (url, depth)
    queue: list[tuple[str, int]] = [(base_url, 0)]

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; ScholarshipBot/1.0)"},
        follow_redirects=True,
    ) as client:
        while queue:
            url, depth = queue.pop(0)
            url = _normalize_url(url)

            if url in visited:
                continue
            visited.add(url)

            if depth > max_depth:
                continue

            print(f"  Crawling [{depth}] {url}")

            # Fetch page
            html = None
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

            # Try static fetch first (faster)
            html = await _fetch_static(url, client)

            # Fall back to Playwright for JS-heavy pages
            if html is None or len(html) < 500:
                html = await _fetch_with_playwright(url)

            if not html:
                continue

            # Extract text
            soup = BeautifulSoup(html, "lxml")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)

            # Only store pages with keywords
            if _contains_keywords(text):
                page_type = _classify_page(url, text)
                results.append({
                    "url": url,
                    "html_text": text[:50000],  # cap at 50k chars
                    "page_type": page_type,
                    "school": school,
                })

            if depth < max_depth:
                # Enqueue internal links
                links = _extract_links(url, html, internal_links_only)
                # Prioritize likely-relevant pages
                priority = [l for l in links if _is_priority_url(l) and l not in visited]
                regular = [l for l in links if not _is_priority_url(l) and l not in visited]
                for link in priority + regular:
                    queue.append((link, depth + 1))

                # Also fetch PDF links
                for pdf_url in _extract_pdf_links(url, html):
                    if pdf_url not in visited:
                        queue.append((pdf_url, depth + 1))

    print(f"  Done crawling {school}: {len(visited)} pages visited, {len(results)} flagged")
    return results


async def crawl_all_sites(
    targets: list[str],
    max_depth: int = 5,
    internal_links_only: bool = True,
    keyword_flags: Optional[list] = None,
) -> dict[str, list[dict]]:
    """Crawl all target sites. Returns {base_url: [pages]}."""
    all_results = {}
    for url in targets:
        print(f"\nCrawling site: {url}")
        try:
            pages = await crawl_site(
                url,
                max_depth=max_depth,
                internal_links_only=internal_links_only,
                keyword_flags=keyword_flags,
            )
            all_results[url] = pages
        except Exception as e:
            print(f"  ERROR crawling {url}: {e}")
            all_results[url] = []
    return all_results
