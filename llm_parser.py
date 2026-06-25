"""
llm_parser.py — Uses Claude to extract and filter scholarship opportunities.

Calls claude-sonnet-4-6 via the Anthropic Python SDK.
"""

import json
import os
import re
import time
from typing import Optional

import anthropic

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MODEL = "claude-sonnet-4-6"

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
    """Strip markdown code fences if present."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def extract_opportunities(page: dict, retries: int = 2) -> list[dict]:
    """
    Extract opportunities from a single crawled page using Claude.
    Only calls API if page contains keyword flags.
    Returns list of opportunity dicts.
    """
    text = page.get("html_text", "")
    url = page.get("url", "")
    school = page.get("school", "")

    if not text or len(text) < 100:
        return []

    # Truncate to avoid token limits
    text_excerpt = text[:12000]

    prompt = f"Source URL: {url}\nSchool: {school}\n\nPage content:\n{text_excerpt}"

    for attempt in range(retries + 1):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=2048,
                system=EXTRACT_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text
            cleaned = _clean_json(raw)
            opps = json.loads(cleaned)
            if not isinstance(opps, list):
                return []
            # Annotate with school and source url
            for opp in opps:
                opp["school"] = school
                if not opp.get("url"):
                    opp["url"] = url
            return opps
        except json.JSONDecodeError as e:
            print(f"  [JSON parse error] {url}: {e}")
            if attempt == retries:
                return []
            time.sleep(2)
        except anthropic.RateLimitError:
            print(f"  [Rate limit] sleeping 30s...")
            time.sleep(30)
        except Exception as e:
            print(f"  [LLM extract error] {url}: {e}")
            if attempt == retries:
                return []
            time.sleep(3)

    return []


def filter_opportunities(opps: list[dict], retries: int = 2) -> list[dict]:
    """
    Use Claude to classify eligibility_match for each opportunity.
    Returns the same list with eligibility_match field set.
    """
    if not opps:
        return []

    # Batch in chunks of 20 to stay within token limits
    BATCH_SIZE = 20
    all_filtered = []

    for i in range(0, len(opps), BATCH_SIZE):
        batch = opps[i : i + BATCH_SIZE]
        prompt = json.dumps(batch, indent=2)

        for attempt in range(retries + 1):
            try:
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=4096,
                    system=FILTER_SYSTEM,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw = response.content[0].text
                cleaned = _clean_json(raw)
                filtered = json.loads(cleaned)
                if isinstance(filtered, list):
                    all_filtered.extend(filtered)
                    break
                else:
                    all_filtered.extend(batch)
                    break
            except json.JSONDecodeError as e:
                print(f"  [Filter JSON error] batch {i}: {e}")
                if attempt == retries:
                    # Return batch unfiltered with unclear status
                    for opp in batch:
                        opp.setdefault("eligibility_match", "eligibility unclear — verify before applying")
                    all_filtered.extend(batch)
                else:
                    time.sleep(2)
            except anthropic.RateLimitError:
                print(f"  [Rate limit] sleeping 30s...")
                time.sleep(30)
            except Exception as e:
                print(f"  [LLM filter error] batch {i}: {e}")
                if attempt == retries:
                    for opp in batch:
                        opp.setdefault("eligibility_match", "eligibility unclear — verify before applying")
                    all_filtered.extend(batch)
                else:
                    time.sleep(3)

    return all_filtered


def parse_and_filter_pages(pages: list[dict]) -> list[dict]:
    """
    Full pipeline: extract then filter opportunities from a list of pages.
    Deduplicates by (name, school, url).
    """
    all_opps = []
    seen = set()

    for page in pages:
        opps = extract_opportunities(page)
        for opp in opps:
            key = (opp.get("school", ""), opp.get("name", ""), opp.get("url", ""))
            if key not in seen:
                seen.add(key)
                all_opps.append(opp)

    if all_opps:
        all_opps = filter_opportunities(all_opps)

    return all_opps
