"""
database.py — SQLite storage for craft scholarship opportunities.
"""

import sqlite3
import hashlib
import json
from datetime import date
from typing import Optional


DB_PATH = "scholarships.db"


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str = DB_PATH) -> None:
    conn = get_connection(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS opportunities (
            id TEXT PRIMARY KEY,
            school TEXT,
            name TEXT,
            type TEXT,
            disciplines TEXT,
            eligibility TEXT,
            deadline TEXT,
            amount TEXT,
            url TEXT,
            raw_excerpt TEXT,
            first_seen DATE,
            last_seen DATE,
            is_active BOOLEAN,
            eligibility_match TEXT
        )
    """)
    conn.commit()
    conn.close()


def make_id(school: str, name: str, url: str) -> str:
    raw = f"{school}|{name}|{url}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def upsert_opportunity(opp: dict, db_path: str = DB_PATH) -> str:
    """Insert or update an opportunity. Returns 'new' or 'updated'."""
    today = date.today().isoformat()
    school = opp.get("school", "")
    name = opp.get("name", "")
    url = opp.get("url", "")
    opp_id = make_id(school, name, url)

    conn = get_connection(db_path)
    existing = conn.execute(
        "SELECT id FROM opportunities WHERE id = ?", (opp_id,)
    ).fetchone()

    disciplines = opp.get("disciplines")
    if isinstance(disciplines, list):
        disciplines = ", ".join(disciplines)

    eligibility = opp.get("eligibility_requirements")
    if isinstance(eligibility, list):
        eligibility = "; ".join(eligibility)

    if existing:
        conn.execute(
            """UPDATE opportunities
               SET last_seen = ?, is_active = 1,
                   eligibility_match = ?,
                   deadline = ?, amount = ?, raw_excerpt = ?
               WHERE id = ?""",
            (
                today,
                opp.get("eligibility_match", ""),
                opp.get("deadline", ""),
                opp.get("amount_or_benefit", ""),
                opp.get("raw_excerpt", ""),
                opp_id,
            ),
        )
        conn.commit()
        conn.close()
        return "updated"
    else:
        conn.execute(
            """INSERT INTO opportunities
               (id, school, name, type, disciplines, eligibility,
                deadline, amount, url, raw_excerpt, first_seen, last_seen,
                is_active, eligibility_match)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (
                opp_id,
                school,
                name,
                opp.get("type", ""),
                disciplines or "",
                eligibility or "",
                opp.get("deadline", ""),
                opp.get("amount_or_benefit", ""),
                url,
                opp.get("raw_excerpt", ""),
                today,
                today,
                opp.get("eligibility_match", ""),
            ),
        )
        conn.commit()
        conn.close()
        return "new"


def mark_inactive_if_not_seen_today(seen_ids: list[str], db_path: str = DB_PATH) -> int:
    """Mark all active opportunities not in seen_ids as inactive. Returns count."""
    if not seen_ids:
        conn = get_connection(db_path)
        cursor = conn.execute(
            "UPDATE opportunities SET is_active = 0 WHERE is_active = 1"
        )
        count = cursor.rowcount
        conn.commit()
        conn.close()
        return count

    placeholders = ",".join("?" * len(seen_ids))
    conn = get_connection(db_path)
    cursor = conn.execute(
        f"UPDATE opportunities SET is_active = 0 WHERE is_active = 1 AND id NOT IN ({placeholders})",
        seen_ids,
    )
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count


def get_new_today(db_path: str = DB_PATH) -> list[dict]:
    today = date.today().isoformat()
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM opportunities WHERE first_seen = ? AND is_active = 1",
        (today,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_upcoming_deadlines(days: int = 30, db_path: str = DB_PATH) -> list[dict]:
    today = date.today().isoformat()
    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT * FROM opportunities
           WHERE is_active = 1
             AND deadline IS NOT NULL AND deadline != ''
             AND deadline >= ?
           ORDER BY deadline ASC""",
        (today,),
    ).fetchall()
    conn.close()

    from dateutil.parser import parse as parse_date
    from datetime import timedelta
    cutoff = date.today() + timedelta(days=days)
    results = []
    for r in rows:
        try:
            dl = parse_date(r["deadline"], fuzzy=True).date()
            if dl <= cutoff:
                results.append(dict(r))
        except Exception:
            pass
    return results


def get_all_active(db_path: str = DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM opportunities WHERE is_active = 1 ORDER BY school, name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recently_deactivated(db_path: str = DB_PATH) -> list[dict]:
    today = date.today().isoformat()
    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT * FROM opportunities
           WHERE is_active = 0 AND last_seen < ?
           ORDER BY last_seen DESC
           LIMIT 50""",
        (today,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
