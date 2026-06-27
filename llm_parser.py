"""
llm_parser.py — Uses Gemini to extract and filter scholarship opportunities.

All Gemini calls are async via asyncio.to_thread() so they can be
interrupted by asyncio.wait_for(). Includes per-call 30s timeout,
1s rate-limit delay between calls, and a 150-page batch cap.
"""

import asyncio
import json
import os
import re
import time
from urllib.parse import urlparse

from google import genai

print("[DEBUG] Initializing Gemini client...")
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
print("[DEBUG] Gemini client ready")

MODEL = "gemini-1.5-flash"
LLM_TIMEOUT = 60      # seconds per Gemini call
PAGE_LIMIT = 150      # max pages sent to Gemini per run
RATE_DELAY = 4.0      # seconds between calls (free tier = 15 req/min)
DEBUG_PAGES = 3       # print raw response for first N pages

# Higher score = crawled first
URL_PRIORITY = [
    ("scholarship", 6),
    ("assistantship", 6),
    ("fellowship", 6),
    ("residenc", 6),
    ("financial", 5),
    ("award", 4),
    ("grant", 4),
    ("stipend", 4),
    ("fund", 3),
    ("opportunit", 3),
    ("apply", 2),
    ("support", 1),
    ("tuition", 1),
]

EXTRACT_SYSTEM = """You are a scholarship research assistant specializing in craft and fine arts programs.

Given the text of a webpage, extract every scholarship, fellowship, residency, assistantship, grant, work-study, award, or funded opportunity mentioned.

Return ONLY a valid JSON array (no explanation, no markdown, no code fences). Each element must have these fields:
- name: string (opportunity name)
- type: string (one of: scholarship, fellowship, residency, assistantship, grant, award, work-study, other)
- disciplines: array of strings (e.g. ["glassblowing", "metalsmithing", "jewelry", "ceramics", "blacksmithing", "silversmithing"])
- eligibility_requirements: array of strings (list each requirement)
- deadline: string (date or "rolling" or "unknown")
- amount_or_benefit: string (dollar amount, tuition waiver, housing, etc.)
- url: string (URL where this was found, or empty string)
- raw_excerpt: string (verbatim quote from the page, max 500 chars)

If no opportunities are found, return an empty array: []
Return ONLY the JSON array."""

FILTER_SYSTEM = """You are evaluating craft arts scholarship opportunities for a specific applicant profile.

APPLICANT PROFILE:
- Identity: LGBTQ+ / transgender
- Status: Student or early-career artist
- Disciplines: glassblowing, metalworking, jewelry making, blacksmithing, silversmithing, casting, hotshop work
- Looking for: merit-based awards, identity-based awards (LGBTQ+/trans), field-specific scholarships, work-study, residencies with stipend

INCLUDE if the opportunity is:
- Merit-based
- LGBTQ+/queer/trans identity-based
- For students or early-career artists
- Field-specific (glassblowing, metal, jewelry, blacksmithing, silversmithing, casting, hotshop)
- Work-study programs
- Residencies with stipend or funding

EXCLUDE if the opportunity is:
- Need-based only (requires FAFSA, income verification, or demonstrated financial need as primary criterion)
- K-12 or youth-only (under 18)
- International students only (excludes US citizens/residents)

For each opportunity, set eligibility_match to exactly one of:
- "eligible" — clearly matches the profile and not excluded
- "not eligible" — clearly excluded by criteria above
- "eligibility unclear — verify before applying" — ambiguous or insufficient info

Return ONLY a valid JSON array of the same opportunities with eligibility_match added to each. No explanation."""


def _clean_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _url_priority_score(page: dict) -> int:
    path = urlparse(page.get("url", "")).path.lower()
    score = 0
    for keyword, weight in URL_PRIORITY:
        if keyword in path:
            score += weight
    return score


def _prioritize_pages(pages: list[dict], limit: int = PAGE_LIMIT) -> list[dict]:
    """Sort pages by URL keyword relevance, cap at limit."""
    sorted_pages = sorted(pages, key=_url_priority_score, reverse=True)
    if len(sorted_pages) > limit:
        print(f"  [LLM] Capping at {limit} pages (had {len(sorted_pages)}), prioritizing by URL keywords")
        return sorted_pages[:limit]
    return sorted_pages


async def _gemini_call(prompt: str) -> str:
    """Run a blocking Gemini call in a thread, with 30s timeout."""
    def _sync():
        response = client.models.generate_content(model=MODEL, contents=prompt)
        return response.text

    return await asyncio.wait_for(
        asyncio.to_thread(_sync),
        timeout=LLM_TIMEOUT,
    )


async def extract_opportunities(page: dict, index: int, total: int) -> list[dict]:
    """Extract opportunities from one page. Returns [] on any failure."""
    text = page.get("html_text", "")
    url = page.get("url", "")
    school = page.get("school", "")

    if not text or len(text) < 100:
        return []

    print(f"  [LLM] Parsing page {index}/{total}: {url}")

    prompt = f"{EXTRACT_SYSTEM}\n\nSource URL: {url}\nSchool: {school}\n\nPage content:\n{text[:12000]}"

    try:
        raw = await _gemini_call(prompt)

        if index <= DEBUG_PAGES:
            print(f"  [LLM DEBUG] Raw response (first 500 chars): {raw[:500]}")

        cleaned = _clean_json(raw)
        opps = json.loads(cleaned)

        if not isinstance(opps, list):
            print(f"  [LLM DEBUG] Non-list response for {url}: {type(opps)}")
            return []
        if len(opps) == 0:
            print(f"  [LLM DEBUG] Empty result for {url}")
            return []

        for opp in opps:
            opp["school"] = school
            if not opp.get("url"):
                opp["url"] = url
        print(f"  [LLM] Found {len(opps)} opportunities at {url}")
        return opps
    except asyncio.TimeoutError:
        print(f"  [LLM TIMEOUT] skipped {url} after {LLM_TIMEOUT}s")
        return []
    except json.JSONDecodeError as e:
        print(f"  [LLM JSON error] {url}: {e}")
        print(f"  [LLM DEBUG] Raw that failed to parse: {raw[:300] if 'raw' in dir() else 'N/A'}")
        return []
    except Exception as e:
        if "quota" in str(e).lower() or "rate" in str(e).lower():
            print(f"  [LLM rate limit] sleeping 60s...")
            await asyncio.sleep(60)
        else:
            print(f"  [LLM error] {url}: {type(e).__name__}: {e}")
        return []


async def filter_opportunities(opps: list[dict]) -> list[dict]:
    """Classify eligibility_match for each opportunity. Batches of 20."""
    if not opps:
        return []

    BATCH_SIZE = 20
    all_filtered = []

    for i in range(0, len(opps), BATCH_SIZE):
        batch = opps[i: i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (len(opps) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"  [LLM] Filtering batch {batch_num}/{total_batches} ({len(batch)} opportunities)")

        prompt = f"{FILTER_SYSTEM}\n\n{json.dumps(batch, indent=2)}"
        try:
            raw = await _gemini_call(prompt)
            cleaned = _clean_json(raw)
            filtered = json.loads(cleaned)
            if isinstance(filtered, list):
                all_filtered.extend(filtered)
            else:
                _mark_unclear(batch)
                all_filtered.extend(batch)
        except asyncio.TimeoutError:
            print(f"  [LLM TIMEOUT] filter batch {batch_num} — marking unclear")
            _mark_unclear(batch)
            all_filtered.extend(batch)
        except json.JSONDecodeError as e:
            print(f"  [LLM JSON error] filter batch {batch_num}: {e}")
            _mark_unclear(batch)
            all_filtered.extend(batch)
        except Exception as e:
            if "quota" in str(e).lower() or "rate" in str(e).lower():
                print(f"  [LLM rate limit] sleeping 30s...")
                await asyncio.sleep(30)
            else:
                print(f"  [LLM error] filter batch {batch_num}: {e}")
            _mark_unclear(batch)
            all_filtered.extend(batch)

        if i + BATCH_SIZE < len(opps):
            await asyncio.sleep(RATE_DELAY)

    return all_filtered


def _mark_unclear(opps: list[dict]) -> None:
    for opp in opps:
        opp.setdefault("eligibility_match", "eligibility unclear — verify before applying")


async def parse_and_filter_pages(pages: list[dict]) -> list[dict]:
    """
    Full async pipeline: prioritize → extract → deduplicate → filter.
    Caps at PAGE_LIMIT pages, sorted by URL keyword relevance.
    """
    print(f"[DEBUG] parse_and_filter_pages entered, {len(pages)} pages received")
    pages = _prioritize_pages(pages)
    total = len(pages)
    print(f"[DEBUG] After prioritization: {total} pages to parse")
    all_opps = []
    seen: set[tuple] = set()

    print(f"[DEBUG] Starting page loop...")
    for i, page in enumerate(pages, 1):
        opps = await extract_opportunities(page, i, total)
        for opp in opps:
            key = (opp.get("school", ""), opp.get("name", ""), opp.get("url", ""))
            if key not in seen:
                seen.add(key)
                all_opps.append(opp)
        if i < total:
            await asyncio.sleep(RATE_DELAY)

    if all_opps:
        all_opps = await filter_opportunities(all_opps)

    return all_opps
