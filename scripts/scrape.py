#!/usr/bin/env python3
"""Scrape the data layer for the advising app.

Outputs (all under data/):
  degree_maps.json        semester-by-semester plans for Marketing, Accounting, Management
  sections.json           every Spring 2027 section from the public course search
  path_requirements.json  PATH general-education categories (from the worksheet PDF)
  path_tags.json          course code -> PATH codes it satisfies (Spring 2027 search filters)
  prereqs.json            hand-maintained prerequisite skeleton (existing edits are preserved)
  README.md               scrape date, sources, counts

Usage:
  python scripts/scrape.py                 # live scrape
  python scripts/scrape.py --cache .cache  # reuse/stash raw responses while iterating

Validation failures (semester count, credit totals, PATH PDF contents) abort the
run before anything is written.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode

import pdfplumber
import requests
from bs4 import BeautifulSoup, NavigableString, Tag

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

DEGREE_MAPS = {
    "marketing": {
        "url": "https://www.adelphi.edu/program/undergraduate/marketing/degree-map/",
        "degree": "BS in Marketing",
        "expected_credits": 121,
    },
    "accounting": {
        "url": "https://www.adelphi.edu/program/undergraduate/accounting/degree-map/",
        "degree": "BBA in Accounting",
        "expected_credits": 123,
    },
    "management": {
        "url": "https://www.adelphi.edu/program/undergraduate/management/degree-map/",
        "degree": "BBA in Management",
        "expected_credits": 121,
    },
}

SEARCH_BASE = "https://www.adelphi.edu/academics/course-tools/course-search/"
SEMESTER = "27/02/*"  # Spring 2027
SEMESTER_LABEL = "Spring 2027"
SECTIONS_URL = (
    SEARCH_BASE
    + "?semester=27%2F02%2F*&campus%5B%5D=&level=all&school=all&byKeywords=&distribution=&submitted=1"
)
PAGE_SIZE = 100
PAUSE_SECONDS = 1.0

PATH_PDF_URL = (
    "https://www.adelphi.edu/path/wp-content/uploads/sites/122/2026/09/"
    "WORKSHEET-PATH-PROGRAM-Requirements-9_23_26.pdf"
)

# What the user expects the worksheet to say: code -> courses required.
EXPECTED_PATH = {
    "A": 2, "H": 2, "SS": 2, "NS": 1, "MCL": 1, "G": 2,
    "CW": 1, "CO": 1, "Q": 2, "L": 1, "RJ": 1,
}

# Course-search filter option -> PATH category code.
# "FS" is the search form's internal key for Mathematics, Computing & Logic.
# GL and CE both count toward G (Global Awareness/Civic Engagement).
DIST_TO_PATH = {"A": "A", "FS": "MCL", "H": "H", "NS": "NS", "SS": "SS"}
GOAL_TO_PATH = {"CE": "G", "GL": "G", "CO": "CO", "CW": "CW", "RJ": "RJ", "L": "L", "Q": "Q"}

USER_AGENT = "Mozilla/5.0 (compatible; advising-template-scraper/1.0)"


# --------------------------------------------------------------------------- fetch
class Fetcher:
    def __init__(self, cache_dir: Path | None, pause: float = PAUSE_SECONDS):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.cache_dir = cache_dir
        self.pause = pause
        self._last = 0.0
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, url: str) -> bytes:
        cache_file = None
        if self.cache_dir:
            cache_file = self.cache_dir / hashlib.sha1(url.encode()).hexdigest()
            if cache_file.exists():
                return cache_file.read_bytes()
        wait = self.pause - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(4):
            try:
                r = self.s.get(url, timeout=60)
                r.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(2 ** (attempt + 1))
        self._last = time.monotonic()
        if cache_file:
            cache_file.write_bytes(r.content)
        return r.content


def soup(html: bytes) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


# --------------------------------------------------------------------- course codes
CODE_FULL_RE = re.compile(r"(\d{4})-(\d{3})")


def normalise_code(dept: str, num: str) -> str:
    return f"{dept}-{num}"


def section_to_course(course_number: str) -> str:
    """'0201-101-001' -> '0201-101'."""
    m = re.match(r"(\d{4})-(\d{3})", course_number)
    return normalise_code(*m.groups()) if m else course_number


def parse_code_line(line: str, prefix_map: dict[str, str]) -> list[dict]:
    """Parse a degree-map code line into one or more normalised codes.

    Handles 'ACC 0201-101', 'BFD 0213 102', 'BUS 0204- 380', 'DSC – 271',
    'ACC 201-405', 'SPE 0876-110 or 112', 'SPE 0876-110/112'. Department numbers
    that are missing or malformed are filled from the prefix -> department map
    learned from well-formed codes in the same pages; such codes get fixed=True.
    """
    m = re.match(r"^([A-Z]{2,4})\s*[–—-]?\s*(.+)$", line.strip())
    prefix, rest = m.group(1), m.group(2)
    parts = [p for p in re.split(r"\s+or\s+|/", rest, flags=re.I) if p.strip()]
    out, dept = [], None
    for i, part in enumerate(parts):
        groups = re.findall(r"\d+", part)
        fixed = False
        if len(groups) >= 2:
            d, num = groups[0], groups[1]
            if len(d) != 4:
                d, fixed = prefix_map.get(prefix, d.zfill(4)), True
            dept = d
        else:
            num = groups[0]
            if i == 0 or dept is None:
                dept, fixed = prefix_map.get(prefix), True
        out.append({
            "code": normalise_code(dept, num.zfill(3)),
            "prefix": prefix,
            "code_raw": line.strip(),
            "code_fixed": fixed,
        })
    return out


# ------------------------------------------------------------------- degree maps
def cell_lines(td: Tag) -> list[dict]:
    """Flatten a table cell into lines, splitting on <br> and block tags.

    Each line records whether it was bold and/or italic and any link href, which
    is what distinguishes codes, cohort notes and elective placeholders.
    """
    lines: list[dict] = []
    cur = {"text": "", "bold": False, "italic": False, "href": None}

    def flush():
        nonlocal cur
        t = clean(cur["text"])
        if t:
            cur["text"] = t
            lines.append(cur)
        cur = {"text": "", "bold": False, "italic": False, "href": None}

    def walk(node, bold=False, italic=False, href=None):
        if isinstance(node, NavigableString):
            if node.strip():
                cur["text"] += str(node)
                cur["bold"] |= bold
                cur["italic"] |= italic
                cur["href"] = cur["href"] or href
            elif node:
                cur["text"] += " "
            return
        if not isinstance(node, Tag):
            return
        if node.name == "br":
            flush()
            return
        # Links get their own line so stray CMS text after them isn't glued on.
        block = node.name in ("p", "div", "li", "a")
        if block:
            flush()
        b = bold or node.name in ("strong", "b")
        it = italic or node.name in ("em", "i")
        h = href or (node.get("href") if node.name == "a" else None)
        for c in node.children:
            walk(c, b, it, h)
        if block:
            flush()

    walk(td)
    flush()
    return lines


CODE_LINE_RE = re.compile(r"^[A-Z]{2,4}\s*[–—-]?\s*\d")
REQ_LINE_RE = re.compile(r"Requirement|Learning Goal|^PATH$")
COHORT_RE = re.compile(r"^\(?For students who .*\)?$", re.I)
PAREN_RE = re.compile(r"\s*\(([^)]*)\)\s*")


def parse_credits(text: str) -> tuple[float | None, float | None]:
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text)]
    if not nums:
        return None, None
    return min(nums), max(nums)


def num(x):
    return int(x) if x is not None and float(x).is_integer() else x


def split_parentheticals(title: str) -> tuple[str, list[str]]:
    notes = PAREN_RE.findall(title)
    return clean(PAREN_RE.sub(" ", title)), [clean(n) for n in notes if clean(n)]


def parse_course_block(lines: list[dict], prefix_map) -> list[dict]:
    """A block = optional cohort note(s), a code line, title line(s), requirement line(s)."""
    notes, code_line, title_parts, req_parts = [], None, [], []
    for ln in lines:
        t = ln["text"]
        if COHORT_RE.match(t) or (ln["italic"] and code_line is None):
            notes.append(t.strip("()"))
        elif code_line is None and CODE_LINE_RE.match(t):
            code_line = t
        elif code_line is None:
            continue
        elif req_parts or REQ_LINE_RE.search(t):
            req_parts.append(t)
        elif t.startswith("("):
            notes.append(t.strip("()"))
        elif len(t) > 1:  # drop stray single characters left in the CMS markup
            title_parts.append(t)
    if code_line is None:
        return []
    title, paren_notes = split_parentheticals(" ".join(title_parts))
    notes += paren_notes
    req = clean(" ".join(req_parts).replace("/ ", "/"))
    tags = [clean(x) for x in re.split(r"[,/]", req) if clean(x)]
    codes = parse_code_line(code_line, prefix_map)
    titles = [clean(x) for x in re.split(r"\s+OR\s+", title)]
    if len(titles) != len(codes):
        titles = [title] * len(codes)
    return [
        {**c, "title": titles[i], "requirement": req, "requirement_tags": tags,
         "note": "; ".join(notes)}
        for i, c in enumerate(codes)
    ]


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def parse_degree_row(td: Tag, credit_text: str, prefix_map) -> dict | None:
    lines = cell_lines(td)
    if not lines:
        return None
    first = lines[0]["text"]
    cmin, cmax = parse_credits(credit_text)
    base = {"credits_min": num(cmin), "credits_max": num(cmax), "credits_raw": clean(credit_text)}

    if first.lower().startswith("total for semester"):
        return {"kind": "total", **base}

    if first.upper().startswith("SELECT ONE"):
        segments, current, mode = {"options": [], "also_requires": []}, [], "options"
        for ln in lines[1:]:
            word = ln["text"].upper()
            if ln["bold"] and word in ("OR", "AND"):
                segments[mode].append(current)
                current = []
                if word == "AND":
                    mode = "also_requires"
            else:
                current.append(ln)
        segments[mode].append(current)
        options = [c for seg in segments["options"] for c in parse_course_block(seg, prefix_map)]
        also = [c for seg in segments["also_requires"] for c in parse_course_block(seg, prefix_map)]
        return {"kind": "alternatives", "options": options, "also_requires": also,
                "note": "", **base}

    if not CODE_LINE_RE.match(first) and not COHORT_RE.match(first) and "elective" in first.lower():
        # Elective placeholder: typed slot, no course code.
        rest = [ln for ln in lines[1:] if len(ln["text"]) > 1]
        notes = [ln["text"].strip("()") for ln in rest if ln["text"].startswith("(")]
        link = next((ln for ln in rest if ln["href"]), None)
        req_lines = [ln["text"] for ln in rest if not ln["text"].startswith("(") and not ln["href"]]
        req = clean(" ".join(req_lines).replace("/ ", "/"))
        label = clean(first)
        suggested = sorted({normalise_code(d, n) for note in notes
                            for d, n in re.findall(r"(\d{4})[\s-](\d{3})", note)})
        return {
            "kind": "elective",
            "elective_type": slugify(label),
            "label": label,
            "requirement": req or (link["text"] if link else ""),
            "requirement_tags": [clean(x) for x in re.split(r"[,/]", req) if clean(x)],
            "reference_url": link["href"] if link else None,
            "note": "; ".join(notes),
            "suggested_codes": suggested,
            **base,
        }

    courses = parse_course_block(lines, prefix_map)
    if not courses:
        return {"kind": "unparsed", "raw": " | ".join(ln["text"] for ln in lines), **base}
    if len(courses) == 1:
        c = courses[0]
        return {"kind": "course", **c, **base}
    # "A or B" written inline in the code line, e.g. "SPE 0876-110 or 112".
    note = courses[0]["note"]
    for c in courses:
        c["note"] = ""
    return {"kind": "alternatives", "options": courses, "also_requires": [], "note": note, **base}


def learn_prefix_map(pages: dict[str, BeautifulSoup]) -> dict[str, str]:
    pm: dict[str, str] = {}
    for s in pages.values():
        for strong in s.select("table strong, table b"):
            m = re.match(r"^([A-Z]{2,4})\s+(\d{4})-\d{3}", clean(strong.get_text()))
            if m:
                pm.setdefault(m.group(1), m.group(2))
    return pm


YEAR_NUM = {"first": 1, "second": 2, "third": 3, "fourth": 4}


def parse_degree_map(key: str, s: BeautifulSoup, prefix_map) -> dict:
    info = DEGREE_MAPS[key]
    semesters = []
    for inner in s.select(".accordion_inner"):
        h = inner.select_one(".accordion_title")
        if not h:
            continue
        year_label = clean(h.get_text())
        year = YEAR_NUM.get(year_label.split()[0].lower())
        for item in inner.select(".js-accordion-item"):
            table = item.find("table")
            if table is None:  # e.g. "Extracurricular Activities/Clubs"
                continue
            term = clean(item.select_one(".accordion_item_heading_title").get_text())
            rows, stated = [], None
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) < 2:
                    continue
                row = parse_degree_row(tds[0], tds[1].get_text(" "), prefix_map)
                if row is None:
                    continue
                if row["kind"] == "total":
                    stated = (row["credits_min"], row["credits_max"])
                    continue
                rows.append(row)
            semesters.append({
                "year": year, "year_label": year_label, "term": term,
                "semester_index": len(semesters) + 1,
                "stated_total_min": stated[0] if stated else None,
                "stated_total_max": stated[1] if stated else None,
                "rows": rows,
            })
    text = clean(s.get_text(" "))
    m = re.search(r"Credits to Graduate with an? .+? (\d{3}) ", text)
    return {
        "major": key,
        "degree": info["degree"],
        "source_url": info["url"],
        "page_stated_total_credits": int(m.group(1)) if m else None,
        "semesters": semesters,
    }


def row_credit_range(row: dict) -> tuple[float, float]:
    """Min/max credits a row contributes. Single courses limited to one entry cohort
    (e.g. the 0-credit BFD seminars, 1-credit consulting project) are optional for min."""
    lo, hi = row["credits_min"] or 0, row["credits_max"] or 0
    if row["kind"] == "course" and row.get("note") and COHORT_RE.match(row["note"]):
        lo = 0
    return lo, hi


def validate_degree_map(dm: dict) -> list[str]:
    errors = []
    key = dm["major"]
    expected = DEGREE_MAPS[key]["expected_credits"]
    n = len(dm["semesters"])
    if n != 8:
        errors.append(f"{key}: found {n} semesters, expected 8")
    stated_lo = sum(s["stated_total_min"] or 0 for s in dm["semesters"])
    stated_hi = sum(s["stated_total_max"] or 0 for s in dm["semesters"])
    row_lo = sum(row_credit_range(r)[0] for s in dm["semesters"] for r in s["rows"])
    row_hi = sum(row_credit_range(r)[1] for s in dm["semesters"] for r in s["rows"])
    dm["credit_check"] = {
        "expected": expected,
        "page_stated_total": dm["page_stated_total_credits"],
        "sum_of_semester_totals": [num(stated_lo), num(stated_hi)],
        "sum_of_rows": [num(row_lo), num(row_hi)],
    }
    if dm["page_stated_total_credits"] != expected:
        errors.append(f"{key}: page states {dm['page_stated_total_credits']} credits, expected {expected}")
    if not (stated_lo - 1 <= expected <= stated_hi + 1):
        errors.append(f"{key}: semester totals sum to {stated_lo}-{stated_hi}, expected ~{expected}")
    for sem in dm["semesters"]:
        for r in sem["rows"]:
            if r["kind"] == "unparsed":
                errors.append(f"{key} {sem['year_label']} {sem['term']}: unparsed row {r['raw']!r}")
    return errors


def degree_map_courses(dm: dict) -> list[dict]:
    """Every concrete course (not elective slots) in a degree map."""
    out = []
    for sem in dm["semesters"]:
        for r in sem["rows"]:
            if r["kind"] == "course":
                out.append(r)
            elif r["kind"] == "alternatives":
                out += r["options"] + r["also_requires"]
    return out


# -------------------------------------------------------------------- course search
def search_url(**params) -> str:
    q = {"semester": SEMESTER, "campus[]": "", "level": "all", "school": "all",
         "byKeywords": "", "distribution": ""}
    q.update(params)
    q["submitted"] = "1"
    return SEARCH_BASE + "?" + urlencode(q, doseq=True)


TIME_RE = re.compile(r"(\d{1,2}:\d{2})\s*(am|pm|noon)?", re.I)


def to_24h(hm: str, mer: str | None) -> str:
    h, m = map(int, hm.split(":"))
    mer = (mer or "").lower()
    if mer == "pm" and h != 12:
        h += 12
    if mer == "am" and h == 12:
        h = 0
    return f"{h:02d}:{m:02d}"


def parse_time(text: str) -> tuple[str | None, str | None]:
    hits = TIME_RE.findall(text or "")
    if len(hits) < 2:
        return None, None
    (s, sm), (e, em) = hits[0], hits[1]
    em = em or ""
    if em.lower() == "noon":
        em = "pm"
    sm = sm or em  # "10:00 - 11:15 am"
    if sm.lower() == "noon":
        sm = "pm"
    return to_24h(s, sm), to_24h(e, em)


DAYS_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_RE = re.compile("|".join(DAYS_ORDER))


def parse_results_page(html: bytes) -> tuple[list[dict], int | None]:
    s = soup(html)
    label = s.select_one(".course-search-results-label")
    total = None
    if label:
        m = re.search(r"of\s+(\d+)", label.get_text())
        total = int(m.group(1)) if m else None
    out = []
    for table in s.select("table.course-search-results"):
        title = clean(table.caption.get_text()) if table.caption else ""
        for header in table.select("tr.tbl-accordion-header"):
            tds = header.find_all("td")
            number = clean(tds[0].get_text())
            # Multiple meeting patterns are separated by <br> in the Day and Time cells.
            day_lines = [clean(x) for x in tds[1].get_text("|").split("|") if clean(x)]
            days_raw = "; ".join(day_lines)
            credits_raw = clean(tds[2].get_text())
            body = header.find_next_sibling("tr", class_="tbl-accordion-body")
            details, time_lines, status_text = {}, [], None
            if body:
                st = body.select_one(".statusText")
                status_text = clean(st.get_text(" ")) if st else None
                for div in body.select(".fieldsBodyText"):
                    lab = div.select_one(".description-title")
                    if not lab:
                        continue
                    k = clean(lab.get_text()).rstrip(":")
                    lab.extract()
                    if k == "Time":
                        time_lines = [clean(x) for x in div.get_text("|").split("|") if clean(x)]
                    details[k] = clean(div.get_text(" "))
            meetings = []
            for i in range(max(len(day_lines), len(time_lines))):
                d = day_lines[i] if i < len(day_lines) else None
                t = time_lines[i] if i < len(time_lines) else None
                ms, me = parse_time(t or "")
                meetings.append({"days": DAY_RE.findall(d or ""), "start_time": ms, "end_time": me})
            meetings = [m for m in meetings if m["days"] or m["start_time"]]
            start, end = (meetings[0]["start_time"], meetings[0]["end_time"]) if meetings else (None, None)
            if status_text is None:
                status = "open"
            elif "fill" in status_text.lower() or "full" in status_text.lower():
                status = "full"
            elif "cancel" in status_text.lower():
                status = "cancelled"
            else:
                status = "closed"
            cmin, cmax = parse_credits(credits_raw)
            link = tds[0].find("a")
            out.append({
                "course_number": number,
                "course_code": section_to_course(number),
                "section": number.split("-")[2] if number.count("-") >= 2 else None,
                "title": title,
                "days": sorted({d for m in meetings for d in m["days"]}, key=DAYS_ORDER.index),
                "days_raw": days_raw,
                "time_raw": "; ".join(time_lines) or None,
                "start_time": start,
                "end_time": end,
                "meetings": meetings,
                "credits": num(cmin) if cmin == cmax else None,
                "credits_min": num(cmin),
                "credits_max": num(cmax),
                "credits_raw": credits_raw,
                "instructor": details.get("Instructor"),
                "location": details.get("Location"),
                "dates": details.get("Dates/Semester"),
                "status": status,
                "status_text": status_text,
                "detail_url": SEARCH_BASE + link["href"] if link and link.get("href") else None,
                "_details": details,
            })
    return out, total


def fetch_all_pages(f: Fetcher, label: str, **params) -> list[dict]:
    rows, start, total = [], 0, None
    while True:
        url = search_url(**params, startrow=start) if start else search_url(**params)
        page, t = parse_results_page(f.get(url))
        total = total if total is not None else t
        rows += page
        print(f"  {label}: startrow={start} -> {len(page)} rows (total {total})", file=sys.stderr)
        start += PAGE_SIZE
        if not page or len(page) < PAGE_SIZE or (total is not None and start >= total):
            break
    if total is not None and len(rows) != total:
        print(f"  WARNING {label}: got {len(rows)} rows but page reported {total}", file=sys.stderr)
    return rows


# ---------------------------------------------------------------------- PATH PDF
PATH_LINE_RE = re.compile(
    r"^(?P<name>[A-Za-z ,/&]+?)\s*\((?P<code>[A-Z/]+)\)\s*(?P<count>.+?)(?:\s*\[(?P<note>[^\]]+)\])?$"
)


def parse_path_pdf(pdf_bytes: bytes) -> tuple[list[dict], list[str]]:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    cats, group, required = [], None, []
    for raw in text.splitlines():
        line = clean(raw)
        if line.startswith("DISTRIBUTION COURSES"):
            group = "distribution"
            continue
        if line.startswith("UNIVERSITY LEARNING GOALS"):
            group = "learning_goal"
            continue
        for m in re.finditer(r"_+\s*([^_]+?\(\d+ Credits\))", line):
            required.append(clean(m.group(1)))
        m = PATH_LINE_RE.match(line)
        if not m or group is None:
            continue
        name, code, count, note = m["name"].strip(), m["code"], m["count"], m["note"] or ""
        if code == "CW/CO":  # "Communication (CW/CO) 1 Written/1 Oral"
            w = re.search(r"(\d+)\s*Written", count)
            o = re.search(r"(\d+)\s*Oral", count)
            cats.append({"code": "CW", "name": "Written Communication", "group": group,
                         "courses_required": int(w.group(1)), "note": note})
            cats.append({"code": "CO", "name": "Oral Communication", "group": group,
                         "courses_required": int(o.group(1)), "note": note})
            continue
        n = re.match(r"(\d+)\s+courses?", count)
        if not n:
            continue
        cats.append({"code": code, "name": name, "group": group,
                     "courses_required": int(n.group(1)), "note": note})
    return cats, required


def validate_path(cats: list[dict]) -> list[str]:
    found = {c["code"]: c["courses_required"] for c in cats}
    errors = []
    for code, n in EXPECTED_PATH.items():
        if code not in found:
            errors.append(f"PATH PDF: category {code} missing")
        elif found[code] != n:
            errors.append(f"PATH PDF: {code} requires {found[code]} courses, expected {n}")
    for code in found.keys() - EXPECTED_PATH.keys():
        errors.append(f"PATH PDF: unexpected category {code}")
    return errors


# ------------------------------------------------------------------- main pipeline
def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


PREREQ_LABEL_RE = re.compile(r"pre-?req|co-?req", re.I)


def build_prereqs(dm_courses: dict[str, str], sections: list[dict], existing: dict) -> dict:
    scraped: dict[str, list[str]] = {}
    for sec in sections:
        for k, v in sec["_details"].items():
            if PREREQ_LABEL_RE.search(k):
                codes = [normalise_code(d, n) for d, n in CODE_FULL_RE.findall(v)]
                scraped.setdefault(sec["course_code"], [])
                scraped[sec["course_code"]] += [c for c in codes if c not in scraped[sec["course_code"]]]
    out = {}
    for code in sorted(dm_courses):
        if code in existing:  # never clobber hand edits
            out[code] = existing[code]
            continue
        entry = {"title": dm_courses[code], "requires": [], "one_of": [], "notes": ""}
        if scraped.get(code):
            entry["requires"] = scraped[code]
            entry["notes"] = "scraped, unverified"
        out[code] = entry
    # keep any hand-added courses that are not on a degree map
    for code in sorted(existing.keys() - out.keys()):
        out[code] = existing[code]
    return dict(sorted(out.items())), len(scraped)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", type=Path, help="directory to cache raw HTTP responses")
    args = ap.parse_args()
    f = Fetcher(args.cache)
    scraped_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

    # 1. degree maps ---------------------------------------------------------
    print("Degree maps", file=sys.stderr)
    pages = {k: soup(f.get(v["url"])) for k, v in DEGREE_MAPS.items()}
    prefix_map = learn_prefix_map(pages)
    degree_maps, errors = {}, []
    for key, s in pages.items():
        dm = parse_degree_map(key, s, prefix_map)
        errors += validate_degree_map(dm)
        degree_maps[key] = dm

    # 3. PATH requirements (cheap; validate before the long section scrape) ---
    print("PATH worksheet", file=sys.stderr)
    path_cats, path_required_courses = parse_path_pdf(f.get(PATH_PDF_URL))
    errors += validate_path(path_cats)

    if errors:
        print("\nSTOPPING - validation failed:", file=sys.stderr)
        for e in errors:
            print("  - " + e, file=sys.stderr)
        return 1

    # 2. Spring 2027 sections --------------------------------------------------
    print("Sections", file=sys.stderr)
    sections = fetch_all_pages(f, "all")

    # 4. PATH tags ---------------------------------------------------------------
    print("PATH tags", file=sys.stderr)
    tag_sets: dict[str, set[str]] = {}
    option_counts = {}
    for opt, path_code in DIST_TO_PATH.items():
        rows = fetch_all_pages(f, f"distribution={opt}", distribution=opt)
        option_counts[f"distribution={opt}"] = len({r["course_code"] for r in rows})
        for r in rows:
            tag_sets.setdefault(r["course_code"], set()).add(path_code)
    for opt, path_code in GOAL_TO_PATH.items():
        rows = fetch_all_pages(f, f"learnGoals={opt}", **{"learnGoals[]": opt})
        option_counts[f"learnGoals={opt}"] = len({r["course_code"] for r in rows})
        for r in rows:
            tag_sets.setdefault(r["course_code"], set()).add(path_code)
    order = list(EXPECTED_PATH)
    path_tags = {c: sorted(v, key=order.index) for c, v in sorted(tag_sets.items())}

    # 6. prereq skeleton ---------------------------------------------------------
    dm_courses: dict[str, str] = {}
    for dm in degree_maps.values():
        for c in degree_map_courses(dm):
            dm_courses.setdefault(c["code"], c["title"])
    prereq_path = DATA / "prereqs.json"
    existing = json.loads(prereq_path.read_text()) if prereq_path.exists() else {}
    prereqs, n_scraped_prereq = build_prereqs(dm_courses, sections, existing)

    # write ------------------------------------------------------------------
    DATA.mkdir(exist_ok=True)
    for sec in sections:
        sec.pop("_details", None)
    write_json(DATA / "degree_maps.json", {
        "scraped_at": scraped_at,
        "code_format": "DDDD-NNN (department-number); join to sections.course_code",
        "majors": degree_maps,
    })
    write_json(DATA / "sections.json", {
        "scraped_at": scraped_at,
        "semester": SEMESTER_LABEL,
        "source_url": SECTIONS_URL,
        "status_note": "status comes from the section's status banner in 'Expand details' "
                       "('This Course is Filled to Capacity' -> full, 'Closed' -> closed); "
                       "no banner -> open. Raw banner text is in status_text.",
        "count": len(sections),
        "sections": sections,
    })
    write_json(DATA / "path_requirements.json", path_cats)
    write_json(DATA / "path_tags.json", path_tags)
    write_json(prereq_path, prereqs)

    # 8. summary + README -----------------------------------------------------
    section_codes = {s["course_code"] for s in sections}
    per_major = {}
    for key, dm in degree_maps.items():
        codes = sorted({c["code"] for c in degree_map_courses(dm)})
        slots = sum(1 for s in dm["semesters"] for r in s["rows"] if r["kind"] == "elective")
        per_major[key] = {
            "courses": len(codes),
            "elective_slots": slots,
            "missing": [c for c in codes if c not in section_codes],
            "credit_check": dm["credit_check"],
        }
    missing_all = sorted(c for c in dm_courses if c not in section_codes)
    dm_tagged = sorted(c for c in dm_courses if c in path_tags)

    lines = [
        f"Sections scraped ({SEMESTER_LABEL}): {len(sections)} ("
        + ", ".join(f"{n} {k}" for k, n in sorted(Counter(s["status"] for s in sections).items()))
        + "); "
        f"{len(section_codes)} distinct courses",
    ]
    for key, pm in per_major.items():
        cc = pm["credit_check"]
        lines.append(
            f"{key.title()}: {pm['courses']} unique courses + {pm['elective_slots']} elective slots; "
            f"8 semesters; semester totals {cc['sum_of_semester_totals'][0]}-{cc['sum_of_semester_totals'][1]} "
            f"credits (page states {cc['page_stated_total']})"
        )
    lines.append(f"Unique degree-map courses (all majors): {len(dm_courses)}")
    lines.append("PATH categories found: " + ", ".join(
        f"{c['code']}={c['courses_required']}" for c in path_cats))
    lines.append(f"Courses with PATH tags (Spring 2027): {len(path_tags)} "
                 f"({len(dm_tagged)} of them on a degree map)")
    lines.append(f"Prerequisites pre-filled from section details: {n_scraped_prereq} courses")
    lines.append(f"Degree-map courses with no {SEMESTER_LABEL} section ({len(missing_all)}):")
    for c in missing_all:
        majors = [k for k, pm in per_major.items() if c in pm["missing"]]
        lines.append(f"  {c}  {dm_courses[c]}  [{', '.join(majors)}]")
    summary = "\n".join(lines)
    print("\n" + summary)

    readme = f"""# Advising data

Generated by `scripts/scrape.py`. Re-run with `python scripts/scrape.py`.

- **Scraped:** {scraped_at}
- **Join key:** course codes are normalised to `DDDD-NNN`. Degree-map `ACC 0201-101`
  and section `0201-101-001` both become `0201-101`.

## Files

| File | Contents | Count |
|---|---|---|
| `degree_maps.json` | Semester plans for 3 majors (rows: course, alternatives, elective slot) | {', '.join(f"{k}: {v['courses']} courses" for k, v in per_major.items())} |
| `sections.json` | {SEMESTER_LABEL} sections | {len(sections)} sections, {len(section_codes)} courses |
| `path_requirements.json` | PATH categories | {len(path_cats)} categories |
| `path_tags.json` | course code -> PATH codes (from search filters) | {len(path_tags)} courses |
| `prereqs.json` | Hand-maintained prerequisite skeleton | {len(prereqs)} courses |

## Sources

- Degree maps:
{chr(10).join(f"  - {v['url']}" for v in DEGREE_MAPS.values())}
- Sections: {SECTIONS_URL}
  (100 per page via `startrow`; 1-second pause between requests)
- PATH requirements: {PATH_PDF_URL}
- PATH tags: the same course search re-run once per filter option. Option -> PATH code:
  `distribution` {', '.join(f'{k}->{v}' for k, v in DIST_TO_PATH.items())};
  `learnGoals[]` {', '.join(f'{k}->{v}' for k, v in GOAL_TO_PATH.items())}.

## Caveats

- **Section status** comes from the banner in each row's "Expand details": `full` ("This Course is
  Filled to Capacity"), `closed` ("Closed"), or `open` (no banner). `status_text` keeps the raw banner.
- **Multiple meeting patterns** (e.g. Mon/Wed/Fri plus a Tue/Thu lab) are listed in `meetings`.
  Top-level `start_time`/`end_time` are the first meeting's; `days` is the union.
- **PATH tags** cover only courses offered in {SEMESTER_LABEL}. A course not offered that term has no tags.
- **Prerequisites:** neither the result rows' "Expand details" nor the section detail pages list
  prerequisites, so `prereqs.json` is an empty skeleton. Re-running the scraper keeps your hand edits
  and adds any new degree-map courses.
- The PATH worksheet also requires {' and '.join(path_required_courses)}. These are
  required courses, not categories, so they aren't in `path_requirements.json`.
- Degree-map codes with a missing or garbled department number (e.g. `DSC – 271`, `BUS 020- 380`,
  `ACC 201-405`) are repaired from the prefix's department number. They're flagged `code_fixed: true`
  and keep `code_raw`.

## Credit check

| Major | Page states | Sum of semester totals | Expected |
|---|---|---|---|
{chr(10).join(f"| {k.title()} | {v['credit_check']['page_stated_total']} | {v['credit_check']['sum_of_semester_totals'][0]}-{v['credit_check']['sum_of_semester_totals'][1]} | {v['credit_check']['expected']} |" for k, v in per_major.items())}

The ranges come from cohort-dependent rows: 3-vs-4-credit DSC options, and the BFD and BUS 380
items that apply only to some entry years.

## Summary from last run

```
{summary}
```
"""
    (DATA / "README.md").write_text(readme)
    return 0


if __name__ == "__main__":
    sys.exit(main())
