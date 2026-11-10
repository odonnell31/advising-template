#!/usr/bin/env python3
"""Validate data/prereqs.json against the scraped data. Never changes entries.

Checks
  1. Valid JSON; each entry has title (str), requires (list), one_of (list), notes (str).
     one_of may be a flat list of codes (any one satisfies) or a list of such lists
     (each inner list is a separate "any one of" group).
  2. Every code in a key, requires, or one_of exists in degree_maps.json or sections.json.
  3. No course requires itself; no circular chains (requires + one_of edges).
  4. Lists courses where requires and one_of are both empty (confirm they truly have none).
  5. For each major, walks the degree map in semester order and flags courses placed
     before (or in the same semester as) their prerequisites.

Usage:
  python scripts/validate_prereqs.py            # report only
  python scripts/validate_prereqs.py --format   # also rewrite with canonical formatting
                                                # (2-space indent, sorted top-level keys)
Exit code 1 if any ERROR is found; empty entries and ordering warnings don't fail.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PREREQS = DATA / "prereqs.json"
CODE_RE = re.compile(r"^\d{4}-\d{3}$")
FIELDS = {"title": str, "requires": list, "one_of": list, "notes": str}


def canonical(obj: dict) -> str:
    return json.dumps(dict(sorted(obj.items())), indent=2, ensure_ascii=False) + "\n"


def one_of_groups(one_of: list) -> list[list[str]]:
    if not one_of:
        return []
    if all(isinstance(x, list) for x in one_of):
        return one_of
    return [one_of]


def degree_map_positions(dm: dict) -> dict[str, int]:
    """code -> first semester_index where it appears (as a course, option, or also_requires)."""
    pos: dict[str, int] = {}
    for sem in dm["semesters"]:
        i = sem["semester_index"]
        for r in sem["rows"]:
            codes = []
            if r["kind"] == "course":
                codes = [r["code"]]
            elif r["kind"] == "alternatives":
                codes = [c["code"] for c in r["options"] + r["also_requires"]]
            for c in codes:
                pos.setdefault(c, i)
    return pos


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    cycles, state, stack = [], {}, []

    def dfs(n):
        state[n] = 1
        stack.append(n)
        for m in sorted(graph.get(n, ())):
            if m == n:
                continue  # self-reference reported separately
            if state.get(m) == 1:
                cyc = stack[stack.index(m):] + [m]
                if sorted(cyc[:-1]) not in [sorted(c[:-1]) for c in cycles]:
                    cycles.append(cyc)
            elif m not in state:
                dfs(m)
        stack.pop()
        state[n] = 2

    for n in sorted(graph):
        if n not in state:
            dfs(n)
    return cycles


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--format", action="store_true", help="rewrite prereqs.json with canonical formatting")
    ap.add_argument("--file", type=Path, default=PREREQS, help="prereqs file to check (default data/prereqs.json)")
    args = ap.parse_args()
    path = args.file.resolve()

    errors, warnings = [], []
    raw = path.read_text()
    try:
        prereqs = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: {path.name} is not valid JSON: {e}")
        return 1
    if not isinstance(prereqs, dict):
        print("ERROR: top level must be an object keyed by course code")
        return 1

    dms = json.loads((DATA / "degree_maps.json").read_text())["majors"]
    sections = json.loads((DATA / "sections.json").read_text())["sections"]
    dm_codes = {c for dm in dms.values() for c in degree_map_positions(dm)}
    known = dm_codes | {s["course_code"] for s in sections}

    # 1-2. schema + code existence -------------------------------------------
    graph: dict[str, set[str]] = {}
    for key, e in prereqs.items():
        if not CODE_RE.match(key):
            errors.append(f"{key}: key is not in DDDD-NNN format")
        if key not in known:
            errors.append(f"{key}: key not found in degree_maps.json or sections.json")
        if not isinstance(e, dict):
            errors.append(f"{key}: entry must be an object")
            continue
        for f, t in FIELDS.items():
            if f not in e:
                errors.append(f"{key}: missing field '{f}'")
            elif not isinstance(e[f], t):
                errors.append(f"{key}: '{f}' must be {t.__name__}")
        for extra in sorted(e.keys() - FIELDS.keys()):
            warnings.append(f"{key}: unexpected field '{extra}'")
        req = e.get("requires") if isinstance(e.get("requires"), list) else []
        oo = e.get("one_of") if isinstance(e.get("one_of"), list) else []
        if oo and not (all(isinstance(x, str) for x in oo) or all(isinstance(x, list) for x in oo)):
            errors.append(f"{key}: one_of must be a list of codes or a list of lists of codes")
        refs = [("requires", c) for c in req] + [("one_of", c) for g in one_of_groups(oo) for c in g]
        for field, c in refs:
            if not isinstance(c, str):
                errors.append(f"{key}: {field} contains non-string {c!r}")
                continue
            if not CODE_RE.match(c):
                errors.append(f"{key}: {field} code {c!r} is not in DDDD-NNN format")
            elif c not in known:
                errors.append(f"{key}: {field} code {c} not found in degree_maps.json or sections.json")
            graph.setdefault(key, set()).add(c)
        if len(req) != len(set(map(str, req))):
            warnings.append(f"{key}: duplicate codes in requires")

    # 3. self-reference + cycles ---------------------------------------------
    for key, deps in graph.items():
        if key in deps:
            errors.append(f"{key}: requires itself")
    for cyc in find_cycles(graph):
        errors.append("circular chain: " + " -> ".join(cyc))

    # 4. empty entries ---------------------------------------------------------
    empty = sorted(k for k, e in prereqs.items()
                   if isinstance(e, dict) and not e.get("requires") and not e.get("one_of"))

    # 5. degree-map order ------------------------------------------------------
    order_issues: dict[str, list[str]] = {}
    for major, dm in dms.items():
        pos = degree_map_positions(dm)
        sem_label = {s["semester_index"]: f"Y{s['year']} {s['term']}" for s in dm["semesters"]}
        issues = []
        for code, i in sorted(pos.items(), key=lambda x: (x[1], x[0])):
            e = prereqs.get(code)
            if not isinstance(e, dict):
                continue
            here = f"{code} ({sem_label[i]})"
            for p in e.get("requires") or []:
                if p == code:
                    continue  # reported as an error above
                if p not in pos:
                    issues.append(f"{here} requires {p}, which is not on the {major} map")
                elif pos[p] > i:
                    issues.append(f"{here} comes BEFORE its prerequisite {p} ({sem_label[pos[p]]})")
                elif pos[p] == i:
                    issues.append(f"{here} is in the SAME semester as prerequisite {p} (ok only if corequisite)")
            for g in one_of_groups(e.get("one_of") or []):
                earlier = [p for p in g if p in pos and pos[p] < i]
                if not earlier:
                    on_map = [f"{p} ({sem_label[pos[p]]})" for p in g if p in pos]
                    issues.append(f"{here} needs one of {g}; none is placed earlier"
                                  + (f" (on map: {', '.join(on_map)})" if on_map else " (none on map)"))
        order_issues[major] = issues

    # report ------------------------------------------------------------------
    print(f"{path.name}: {len(prereqs)} entries, valid JSON")
    print(f"\nErrors ({len(errors)}):")
    print("\n".join(f"  - {x}" for x in errors) or "  none")
    print(f"\nWarnings ({len(warnings)}):")
    print("\n".join(f"  - {x}" for x in warnings) or "  none")
    print(f"\nEmpty requires AND one_of ({len(empty)}) - confirm these truly have none:")
    for k in empty:
        print(f"  {k}  {prereqs[k].get('title', '')}")
    for major, issues in order_issues.items():
        print(f"\nDegree-map order, {major} ({len(issues)}):")
        print("\n".join(f"  - {x}" for x in issues) or "  none")

    formatted = canonical(prereqs)
    if raw != formatted:
        if args.format:
            path.write_text(formatted)
            print(f"\nFormatting: rewrote {path.name} (indent, key order, trailing newline only)")
        else:
            print(f"\nFormatting: {path.name} is not canonically formatted (run with --format)")
    else:
        print(f"\nFormatting: {path.name} already canonical, no changes")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
