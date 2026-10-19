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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

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


# ------------------------------------------------------------------ posting age
def age_days(posted):
    """Best-effort age of a posting in days; None if unknown."""
    if not posted:
        return None
    p = str(posted).strip()
    pl = p.lower()
    if "today" in pl or "just posted" in pl:
        return 0
    if "yesterday" in pl:
        return 1
    m = re.search(r"(\d+)\+?\s*days?", pl)
    if m:
        return int(m.group(1))
    try:
        if re.match(r"\d{4}-\d{2}-\d{2}", p):
            iso = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", p.replace("Z", "+00:00"))
            iso = re.sub(r"\.\d+", "", iso)  # drop fractional seconds (py3.10 compat)
            dt = datetime.fromisoformat(iso)
        else:
            dt = parsedate_to_datetime(p)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
    except Exception:
        return None



def posted_text(posted):
    """'3 h ago (Oct 18)', '2 days ago (Oct 16)', '30+ days ago', or 'date not listed'."""
    a = age_days(posted)
    if a is None:
        return "date not listed"
    p = str(posted).strip()
    if re.search(r"\d+\+\s*days", p.lower()):
        return re.search(r"\d+\+", p).group(0) + " days ago"
    date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", p)) or "day" in p.lower()
    if date_only:
        txt = "today" if a < 1 else "yesterday" if a < 2 else f"{int(a)} days ago"
    elif a < 1 / 24:
        txt = f"{max(1, int(a * 1440))} min ago"
    elif a < 1:
        txt = f"{int(a * 24)} h ago"
    else:
        txt = f"{int(a)} day{'s' if int(a) != 1 else ''} ago"
    if not re.search(r"day|today|yesterday", p.lower()):
        try:
            when = datetime.now(timezone.utc).timestamp() - a * 86400
            txt += datetime.fromtimestamp(when, timezone.utc).strftime(" (%b %-d)")
        except Exception:
            pass
    return txt


# ------------------------------------------------------------------ location
US_STATES = ["alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
             "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
             "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri",
             "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york",
             "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
             "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington",
             "west virginia", "wisconsin", "wyoming"]
# 2-letter US state codes after a comma; "CA" left out because Workday/SuccessFactors use it for Canada
US_CODES = re.compile(r",\s*(AL|AK|AZ|AR|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|"
                      r"NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b")
FOREIGN = ["united states", "usa", "u.s.", "(us)", "united kingdom", "uk", "england", "scotland", "germany",
           "india", "china", "taiwan", "japan", "korea", "israel", "mexico", "ireland", "france", "netherlands",
           "singapore", "australia", "new zealand", "poland", "romania", "serbia", "costa rica", "malaysia",
           "philippines", "brazil", "spain", "italy", "switzerland", "sweden", "denmark", "belgium", "vietnam"]


def _segments(loc):
    return [p.strip() for p in re.split(r"\s*(?:\||;|\n| / )\s*", loc or "") if p.strip()]


def _foreign(seg):
    s = seg.lower()
    return bool(_has(s, FOREIGN) or _has(s, US_STATES) or US_CODES.search(seg))


def segment_in_area(seg, geo):
    """One location string like 'Cambridge (US), Massachusetts, United States' -> False."""
    s = seg.lower()
    if _foreign(seg) or _has(s, geo["exclude"]):
        return None
    city = _has(s, geo["include"])
    if city:
        return city
    remote = "remote" in s or "anywhere" in s
    canada = "canada" in s or re.search(r"\b(ontario|on)\b", s) or s in ("ca", "can")
    if remote and canada:
        return "remote (Canada)"
    if re.search(r"\bontario\b", s) or s in ("canada", "ca", "can"):
        return "Ontario/Canada, city unspecified"
    return None


def location_ok(j, geo):
    loc = j.get("location") or ""
    if not loc.strip():
        # unknown location: keep if the company is local (small GTA/KW firm)
        return bool(j.get("local_company")), "no location listed (local company)"
    if "in-area filter applied" in loc:
        return True, "workday area filter"
    segs = _segments(loc)
    for seg in segs:
        hit = segment_in_area(seg, geo)
        if hit:
            return True, hit
    # bare "Remote" segment (no country) next to an in-area city is handled above; lone "Remote":
    if geo.get("allow_unspecified_remote") and any(s.lower() in ("remote", "anywhere") for s in segs) \
            and not any(_foreign(s) for s in segs):
        return True, "remote (unspecified)"
    return False, "outside area"


def area_location(j, geo):
    """Short location for messages: the in-area part only, plus '+N other locations'."""
    segs = _segments(j.get("location") or "")
    good = [s for s in segs if segment_in_area(s, geo)]
    if not segs:
        return "location not listed"
    if not good:
        return segs[0]
    extra = len(segs) - 1
    return good[0] + (f" (+{extra} other location{'s' if extra > 1 else ''})" if extra else "")


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
    strong = _has(t, tf["strong_hardware_terms"])
    # software roles: rejected unless the title is clearly hardware ("Embedded Software" is still software)
    sw = _has(t, tf["software_only"])
    if sw and not strong:
        return False, f"software role ('{sw}')"
    # weak role words (developer, scientist, technologist, designer) need a clear domain word too
    if role in [w.lower() for w in tf["weak_role_terms"]] and not _has(t, tf["weak_role_domain_terms"]):
        return False, f"'{role}' title without a hardware/biomedical domain"
    if j.get("category") in ("hardware", "aggregator"):
        dom = _has(t, tf["hardware_terms"])
        if not dom:
            return False, "not a hardware/PCB title"
        return True, f"{role} + {dom}"
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
    cap = cfg["limits"].get("preferred_years_cap", 2)
    if lo is not None and lo > max_y and (not preferred or lo > cap):
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
- The posting asks for MORE than {max_years} years of experience, whether "required", "preferred" or "an asset" (internships/co-ops/startup work count as experience only if the posting says so). "3-5 years" means minimum 3. Report the smallest number of years it asks for.
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
