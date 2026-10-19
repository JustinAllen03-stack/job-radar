"""Decide whether a posting is worth an alert.

Stage 1 (free, every job):  location + title rules.
Stage 2 (free, survivors):  experience / degree rules on the description.
Stage 3 (optional, cents):  Claude reads the posting next to your profile and
                            catches what regex can't ("minimum five years",
                            "Master's preferred" vs "required", P.Eng required...).
"""
import json
import os
import re

import requests

WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
           "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15}


def _has(text, terms):
    t = f" {text.lower()} "
    for term in terms:
        term = term.lower()
        if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", t):
            return term
    return None


# ------------------------------------------------------------------ location
def location_ok(j, geo):
    loc = (j.get("location") or "").lower()
    if not loc.strip():
        # unknown location: keep if the company is local (small GTA/KW firm)
        return bool(j.get("local_company")), "no location listed (local company)"
    if "in-area filter applied" in loc:
        return True, "workday area filter"
    city = _has(loc, geo["include"])
    if city:
        return True, city
    if _has(loc, geo["exclude"]):
        return False, "outside area"
    remote = "remote" in loc or "anywhere" in loc
    canada = "canada" in loc or re.search(r"\b(ontario|on)\b", loc) or loc.strip() in ("ca", "can")
    if remote and canada:
        return True, "remote (Canada)"
    if remote and geo.get("allow_unspecified_remote") and not re.search(r"\b(us|usa|united states)\b", loc):
        return True, "remote (unspecified)"
    if re.search(r"\bontario\b", loc) or loc.strip() in ("canada", "ca", "can"):
        return True, "Ontario/Canada, city unspecified"
    return False, "outside area"


# ------------------------------------------------------------------ title
def title_ok(j, cfg):
    t = j["title"]
    tf = cfg["titles"]
    bad = _has(t, tf["exclude"])
    if bad and not _has(t, tf.get("exclude_overrides", [])):
        return False, f"title has '{bad}'"
    if re.search(r"\b(III|IV|V|3|4)\s*$", t.strip()) and re.search(r"(engineer|developer|scientist)", t, re.I):
        return False, "senior level number"
    role = _has(t, tf["role_terms"])
    if not role:
        return False, "not an engineering-type title"
    if j.get("category") == "hardware" or j.get("category") == "aggregator":
        dom = _has(t, tf["hardware_terms"])
        if not dom:
            return False, "not a hardware/PCB title"
        return True, f"{role} + {dom}"
    # medtech companies: any engineering role except pure software
    if _has(t, tf["software_only"]) and not _has(t, tf["hardware_terms"]):
        return False, "software-only role"
    return True, role


# ------------------------------------------------------------------ rules on description
YEARS_RE = re.compile(
    r"(?:minimum(?: of)?|at least|min\.?)?\s*"
    r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen)"
    r"\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*(\d{1,2}|\w+)\s*\+?)?\s*\(?\d*\)?\s*"
    r"(?:\+\s*)?(?:years?|yrs?)",
    re.I)


def min_years(text):
    """Smallest 'N years' requirement that is about experience. None if not found."""
    found = []
    for m in YEARS_RE.finditer(text):
        window = text[m.start(): m.end() + 80].lower()
        before = text[max(0, m.start() - 60): m.start()].lower()
        if "experience" not in window and "experience" not in before:
            continue
        if re.search(r"(degree|program|warranty|old|age|company|history|founded|since)", window[:40]):
            continue
        if re.search(r"(degree|bachelor|diploma|program|university|college)[^.;\n]{0,15}$", before):
            continue
        raw = m.group(1).lower()
        n = int(raw) if raw.isdigit() else WORDNUM.get(raw)
        if n is not None and n < 30:
            found.append((n, window + " " + before))
    if not found:
        return None, False
    lo = min(n for n, _ in found)
    # is the smallest requirement only "preferred / an asset"?
    ctxs = [c for n, c in found if n == lo]
    preferred = all(re.search(r"(preferred|an asset|nice to have|bonus|ideally)", c) for c in ctxs)
    return lo, preferred


DEGREE_REQ = re.compile(
    r"(master'?s|m\.?\s?a?\.?sc\.?|m\.?eng\.?|ph\.?\s?d\.?|doctorate|graduate degree)"
    r"[^.\n]{0,80}?(required|is required|mandatory|must)|"
    r"(require[sd]?|must have|minimum)[^.\n]{0,60}?(master'?s|m\.?\s?a?sc|ph\.?\s?d|doctorate)",
    re.I)
BACHELOR_OK = re.compile(r"(bachelor'?s?|b\.?\s?eng|b\.?\s?a?sc|undergraduate)", re.I)


def rules_check(j, cfg):
    text = j.get("description") or ""
    max_y = cfg["limits"]["max_years"]
    lo, preferred = min_years(text)
    if lo is not None and lo > max_y and not preferred:
        return False, f"asks for {lo}+ years", lo
    m = DEGREE_REQ.search(text)
    if m:
        # "Bachelor's or Master's required" is fine; only reject if no bachelor's path is offered nearby
        span = text[max(0, m.start() - 100): m.end() + 40]
        if not BACHELOR_OK.search(span):
            return False, "requires a graduate degree", lo
    if re.search(r"(must|required to) (be|hold)[^.\n]{0,40}(licensed|registered) (as a )?(p\.?\s?eng|professional engineer)", text, re.I) \
            and not re.search(r"\bEIT\b|engineer[- ]in[- ]training", text, re.I):
        return False, "requires P.Eng licence", lo
    return True, ("no experience requirement found" if lo is None else f"asks for {lo} years"
                  + (" (preferred)" if preferred else "")), lo


# ------------------------------------------------------------------ Claude
PROMPT = """You screen job postings for one candidate. Be strict about hard requirements, generous about fit.

CANDIDATE PROFILE
{profile}

HARD RULES (reject if any is true):
- Minimum required professional experience is MORE than {max_years} years (internships/co-ops/startup work count as experience only if the posting says so). "3-5 years" means minimum 3.
- A Master's or PhD is REQUIRED (preferred/an asset is fine). "Bachelor's or Master's" is fine.
- Requires a P.Eng licence with no EIT option, or a security clearance a new Canadian grad couldn't get.
- It is an internship, co-op, or student-only role.
- Role is mainly sales, pure software, or a technician role well below a B.Eng.

POSTING
Company: {company}
Title: {title}
Location: {location}
Text:
{text}

Reply with JSON only:
{{"apply": true/false, "min_years_required": number or null, "degree_required": "bachelor"|"master"|"phd"|"none"|"unknown", "fit": 1-5, "reason": "max 18 words, mention the key requirement or the strongest match"}}"""


def claude_check(j, cfg, profile):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    text = (j.get("description") or "")[:9000]
    body = {
        "model": cfg["claude"]["model"],
        "max_tokens": 200,
        "messages": [{"role": "user", "content": PROMPT.format(
            profile=profile, max_years=cfg["limits"]["max_years"], company=j["company"],
            title=j["title"], location=j["location"], text=text)}],
    }
    r = requests.post("https://api.anthropic.com/v1/messages", json=body, timeout=60, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    r.raise_for_status()
    out = "".join(b.get("text", "") for b in r.json().get("content", []))
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return None
    v = json.loads(m.group(0))
    ok = bool(v.get("apply"))
    if isinstance(v.get("min_years_required"), (int, float)) and v["min_years_required"] > cfg["limits"]["max_years"]:
        ok = False
    if v.get("degree_required") in ("master", "phd"):
        ok = False
    if (v.get("fit") or 0) < cfg["claude"].get("min_fit", 3):
        ok = False
    v["apply"] = ok
    return v
