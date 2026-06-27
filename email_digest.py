"""
email_digest.py — Build and send the weekly HTML scholarship digest via SendGrid.
"""

import os
from datetime import date
from typing import Optional

import sendgrid
from sendgrid.helpers.mail import Mail, To, From, Subject, HtmlContent


BADGE_COLORS = {
    "eligible": "#2d6a4f",
    "not eligible": "#ae2012",
    "eligibility unclear — verify before applying": "#b5838d",
}

BADGE_LABELS = {
    "eligible": "ELIGIBLE",
    "not eligible": "NOT ELIGIBLE",
    "eligibility unclear — verify before applying": "VERIFY ELIGIBILITY",
}


def _badge(match: str) -> str:
    color = BADGE_COLORS.get(match, "#666")
    label = BADGE_LABELS.get(match, match.upper())
    return (
        f'<span style="background:{color};color:#fff;padding:2px 8px;'
        f'border-radius:3px;font-size:11px;font-weight:bold;">{label}</span>'
    )


def _opp_card(opp: dict) -> str:
    match = opp.get("eligibility_match", "")
    disciplines = opp.get("disciplines", "")
    if isinstance(disciplines, list):
        disciplines = ", ".join(disciplines)
    eligibility = opp.get("eligibility", "")
    if isinstance(eligibility, list):
        eligibility = "; ".join(eligibility)

    url = opp.get("url", "")
    link_html = f'<a href="{url}" style="color:#1565c0;">Apply / Learn More</a>' if url else ""

    return f"""
<div style="border:1px solid #ddd;border-radius:6px;padding:16px;margin-bottom:16px;background:#fafafa;">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px;">
    <div>
      <strong style="font-size:16px;">{opp.get('name','(unnamed)')}</strong>
      &nbsp;{_badge(match)}
    </div>
    <div style="color:#555;font-size:13px;">{opp.get('school','')}</div>
  </div>
  <table style="margin-top:10px;width:100%;font-size:13px;border-collapse:collapse;">
    <tr>
      <td style="padding:3px 10px 3px 0;color:#555;white-space:nowrap;vertical-align:top;">Type</td>
      <td style="padding:3px 0;">{opp.get('type','')}</td>
    </tr>
    {"<tr><td style='padding:3px 10px 3px 0;color:#555;white-space:nowrap;vertical-align:top;'>Disciplines</td><td style='padding:3px 0;'>" + disciplines + "</td></tr>" if disciplines else ""}
    {"<tr><td style='padding:3px 10px 3px 0;color:#555;white-space:nowrap;vertical-align:top;'>Eligibility</td><td style='padding:3px 0;'>" + eligibility + "</td></tr>" if eligibility else ""}
    {"<tr><td style='padding:3px 10px 3px 0;color:#555;white-space:nowrap;vertical-align:top;'>Deadline</td><td style='padding:3px 0;font-weight:bold;color:#c0392b;'>" + opp.get('deadline','') + "</td></tr>" if opp.get('deadline') else ""}
    {"<tr><td style='padding:3px 10px 3px 0;color:#555;white-space:nowrap;vertical-align:top;'>Amount/Benefit</td><td style='padding:3px 0;'>" + opp.get('amount','') + "</td></tr>" if opp.get('amount') else ""}
  </table>
  {"<blockquote style='font-style:italic;font-size:12px;color:#444;margin:10px 0 0;border-left:3px solid #ccc;padding-left:10px;'>" + opp.get('raw_excerpt','')[:400] + "…</blockquote>" if opp.get('raw_excerpt') else ""}
  <div style="margin-top:10px;">{link_html}</div>
</div>"""


def _section_header(title: str, color: str = "#1565c0") -> str:
    return f"""
<h2 style="font-family:sans-serif;color:{color};border-bottom:2px solid {color};
    padding-bottom:6px;margin-top:36px;">{title}</h2>"""


def _all_active_table(opps: list[dict]) -> str:
    if not opps:
        return "<p style='color:#666;'>No active opportunities at this time.</p>"

    rows = ""
    for opp in opps:
        match = opp.get("eligibility_match", "")
        rows += f"""<tr>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;">{opp.get('school','')}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;">{opp.get('name','')}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;">{opp.get('deadline','')}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;">{_badge(match)}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;">
            {"<a href='" + opp.get('url','') + "' style='color:#1565c0;'>Link</a>" if opp.get('url') else ''}
          </td>
        </tr>"""

    return f"""
<table style="width:100%;border-collapse:collapse;font-size:13px;">
  <thead>
    <tr style="background:#1565c0;color:#fff;">
      <th style="padding:8px 10px;text-align:left;">School</th>
      <th style="padding:8px 10px;text-align:left;">Opportunity</th>
      <th style="padding:8px 10px;text-align:left;">Deadline</th>
      <th style="padding:8px 10px;text-align:left;">Status</th>
      <th style="padding:8px 10px;text-align:left;">Link</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>"""


def build_html_email(
    new_today: list[dict],
    upcoming: list[dict],
    all_active: list[dict],
    deactivated: list[dict],
) -> str:
    today_str = date.today().strftime("%B %d, %Y")

    new_html = "".join(_opp_card(o) for o in new_today) if new_today else \
        "<p style='color:#666;'>No new opportunities found this week.</p>"

    upcoming_html = "".join(_opp_card(o) for o in upcoming) if upcoming else \
        "<p style='color:#666;'>No deadlines in the next 30 days.</p>"

    deactivated_html = ""
    if deactivated:
        rows = "".join(
            f"<li>{o.get('school','')} — {o.get('name','')} (last seen {o.get('last_seen','')})</li>"
            for o in deactivated
        )
        deactivated_html = f"<ul style='color:#666;font-size:13px;'>{rows}</ul>"
    else:
        deactivated_html = "<p style='color:#666;'>Nothing removed this week.</p>"

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:Georgia,serif;max-width:780px;margin:0 auto;padding:20px;color:#222;">

<div style="background:#1565c0;color:#fff;padding:24px;border-radius:8px;margin-bottom:20px;">
  <h1 style="margin:0;font-size:24px;">Craft Scholarship Finder</h1>
  <p style="margin:8px 0 0;opacity:0.85;">Weekly digest for {today_str}</p>
</div>

<p style="color:#555;font-size:14px;">
  Tracking scholarships, fellowships, residencies, and funded opportunities at craft schools
  across the US — filtered for merit-based, LGBTQ+/trans-affirming, and field-specific
  (glassblowing, metalsmithing, jewelry, blacksmithing, silversmithing, casting) programs.
</p>

{_section_header("1. NEW THIS WEEK", "#2d6a4f")}
{new_html}

{_section_header("2. UPCOMING DEADLINES (next 30 days)", "#c0392b")}
{upcoming_html}

{_section_header("3. ALL ACTIVE OPPORTUNITIES", "#1565c0")}
{_all_active_table(all_active)}

{_section_header("4. REMOVED &amp; EXPIRED", "#666")}
{deactivated_html}

<hr style="margin:40px 0;border:none;border-top:1px solid #ddd;">
<p style="color:#999;font-size:11px;text-align:center;">
  Generated automatically by the Craft Scholarship Finder.
  Data sourced from 30 craft school websites. Verify all details directly with the schools.
  <br>Powered by Gemini gemini-1.5-flash + GitHub Actions.
</p>
</body>
</html>"""


def send_digest(
    html_body: str,
    recipient: str,
    sender: str,
    sendgrid_api_key: Optional[str] = None,
) -> bool:
    """Send the digest email. Returns True on success."""
    api_key = sendgrid_api_key or os.environ.get("SENDGRID_API_KEY")
    if not api_key:
        raise ValueError("SENDGRID_API_KEY not set")

    today_str = date.today().strftime("%B %d, %Y")
    subject = f"Your Weekly Craft Scholarship Digest — {today_str}"

    message = Mail(
        from_email=From(sender),
        to_emails=To(recipient),
        subject=Subject(subject),
        html_content=HtmlContent(html_body),
    )

    sg = sendgrid.SendGridAPIClient(api_key=api_key)
    response = sg.send(message)
    print(f"[EMAIL] SendGrid response: {response.status_code}")
    if response.status_code not in (200, 202):
        print(f"[EMAIL] Error response body: {response.body}")
        print(f"[EMAIL] Error response headers: {dict(response.headers)}")
        return False
    return True
