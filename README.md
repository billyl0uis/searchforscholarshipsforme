# Craft School Scholarship Finder

An automated weekly crawler that finds scholarships, fellowships, residencies, and funded opportunities at craft schools across the US — filtered specifically for merit-based, LGBTQ+/trans-affirming, and field-specific (glassblowing, metalsmithing, jewelry, blacksmithing, silversmithing, casting) programs.

Every Sunday at 9am ET, GitHub Actions crawls 30 craft school websites, uses Claude (claude-sonnet-4-6) to extract and filter opportunities, stores results in SQLite, and sends a formatted HTML email digest.

---

## Setup

### 1. Fork or clone this repo

```bash
git clone https://github.com/billyl0uis/searchforscholarshipsforme.git
cd searchforscholarshipsforme
```

### 2. Add secrets to GitHub Actions

Go to your repo on GitHub:
**Settings -> Secrets and variables -> Actions -> New repository secret**

Add these two secrets:

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key (get one at console.anthropic.com) |
| `SENDGRID_API_KEY` | Your SendGrid API key (get one at sendgrid.com -- free tier works) |

### 3. Trigger a manual run

1. Go to the **Actions** tab in your GitHub repo
2. Click **Craft Scholarship Crawler** in the left sidebar
3. Click **Run workflow** -> **Run workflow**

The run takes 20-60 minutes depending on how many pages are crawled.

### 4. Add or remove schools

Edit `config.yaml` and modify the `targets` list:

```yaml
targets:
  - https://www.urbanglass.org
  - https://penland.org
  # Add new schools here:
  - https://www.example-craft-school.org
```

Commit and push the change -- it will take effect on the next run.

---

## How it works

### Crawler (`crawler.py`)
- Recursively crawls each school's website up to 5 levels deep
- Uses Playwright (headless Chromium) for JavaScript-rendered pages
- Falls back to httpx for static pages
- Parses linked PDFs with pdfplumber
- Only processes pages containing scholarship-related keywords

### LLM Parser (`llm_parser.py`)
- Sends flagged pages to Claude (claude-sonnet-4-6) to extract opportunities as structured JSON
- A second Claude call classifies each opportunity's eligibility match

### Eligibility Profile
The system filters for opportunities that match this profile:

**Include:**
- Merit-based scholarships and awards
- LGBTQ+/queer/trans identity-based funding
- Student or early-career artist opportunities
- Field-specific: glassblowing, metalworking, jewelry, blacksmithing, silversmithing, casting, hotshop
- Work-study programs
- Residencies with stipend or other financial support

**Exclude:**
- Need-based only (requires FAFSA, income verification, demonstrated financial need)
- K-12 / youth-only programs
- International students only

**Flag as "verify before applying":**
- Anything ambiguous or where eligibility is unclear

### Database (`database.py`)
- SQLite database (`scholarships.db`) stores all opportunities
- Persists between weekly runs via GitHub Actions artifacts (90-day retention)
- Opportunities not seen in the current run are marked inactive

### Email Digest (`email_digest.py`)
Weekly HTML email sent via SendGrid with four sections:
1. **NEW THIS WEEK** -- first seen today
2. **UPCOMING DEADLINES** -- deadlines in the next 30 days
3. **ALL ACTIVE** -- condensed table of everything currently tracked
4. **REMOVED & EXPIRED** -- opportunities no longer found on the sites

---

## Files

```
main.py           -- Orchestrator
crawler.py        -- Web crawler (Playwright + httpx + pdfplumber)
llm_parser.py     -- Claude-powered extraction and eligibility filtering
database.py       -- SQLite storage layer
email_digest.py   -- HTML email builder and SendGrid sender
config.yaml       -- School URLs, crawl settings, email addresses
requirements.txt  -- Python dependencies
.github/
  workflows/
    crawl.yml     -- GitHub Actions workflow (runs Sundays 9am ET)
```

## Running locally

```bash
pip install -r requirements.txt
playwright install chromium

export ANTHROPIC_API_KEY=your_key_here
export SENDGRID_API_KEY=your_key_here  # optional for local testing

python main.py
```

If `SENDGRID_API_KEY` is not set, the email digest is saved to `digest_preview.html` instead of sent.

---

## Schools tracked (30 sites)

UrbanGlass, Penland School of Craft, Haystack Mountain School of Crafts, Pilchuck Glass School, Pittsburgh Glass Center, Corning Museum of Glass, Pratt Fine Arts Center, Arrowmont School of Arts and Crafts, Peters Valley School of Craft, John C. Campbell Folk School, Worcester Center for Crafts, Tennessee Tech Craft Center, Southwest School of Art, Ox-Bow School of Art, Watershed Center for the Ceramic Arts, Snow Farm, Anderson Ranch Arts Center, Mendocino Art Center, Touchstone Center for Crafts, Salem Art Works, Sculpture Space, The Steel Yard, North House Folk School, Society of North American Goldsmiths (SNAG), American Craft Council, Jentel Artist Residency, Flux Factory, Brooklyn Metalworks, Res Artis, Alliance of Artist Communities.
