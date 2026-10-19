"""Fetchers for each job-board type.

Every fetcher returns a list of Job dicts:
    {id, company, title, location, url, posted, description}
`description` may be empty; radar.py calls fetch_detail() later, only for
jobs that survive the title/location filters (keeps runs fast).
"""
import html
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus, urljoin

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept": "application/json, text/html;q=0.9, */*;q=0.8"})
TIMEOUT = 25


def strip_html(s):
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def job(src, jid, title, location, url, posted="", description=""):
    return {
        "id": f"{src['key']}:{jid}",
        "company": src["name"],
        "category": src.get("category", ""),
        "local_company": src.get("local_company", False),
        "title": (title or "").strip(),
        "location": (location or "").strip(),
        "url": url,
        "posted": posted or "",
        "description": description or "",
        "_src": src,
        "_raw": {},
    }


def get_json(url, **kw):
    r = S.get(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- Greenhouse
def greenhouse(src):
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{src['id']}/jobs?content=true")
    out = []
    for j in d.get("jobs", []):
        out.append(job(src, j["id"], j.get("title"), (j.get("location") or {}).get("name", ""),
                       j.get("absolute_url"), j.get("first_published") or j.get("updated_at", ""),
                       strip_html(j.get("content", ""))))
    return out


# ---------------------------------------------------------------- Lever
def lever(src):
    d = get_json(f"https://api.lever.co/v0/postings/{src['id']}?mode=json")
    out = []
    for j in d:
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location", "")]
        desc = j.get("descriptionPlain", "") + "\n"
        for lst in j.get("lists", []) or []:
            desc += lst.get("text", "") + "\n" + strip_html(lst.get("content", "")) + "\n"
        desc += j.get("additionalPlain", "") or ""
        posted = ""
        if j.get("createdAt"):
            posted = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(j["createdAt"] / 1000))
        out.append(job(src, j["id"], j.get("text"), " | ".join(filter(None, locs)) + (" | " + j.get("workplaceType", "") if j.get("workplaceType") else ""),
                       j.get("hostedUrl"), posted, desc))
    return out


# ---------------------------------------------------------------- Ashby
def ashby(src):
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{src['id']}?includeCompensation=false")
    out = []
    for j in d.get("jobs", []):
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations", []) or []]
        loc = " | ".join(filter(None, locs))
        if j.get("isRemote"):
            loc += " | Remote"
        out.append(job(src, j["id"], j.get("title"), loc, j.get("jobUrl"), j.get("publishedAt", ""),
                       j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", ""))))
    return out


# ---------------------------------------------------------------- SmartRecruiters
def smartrecruiters(src):
    cid = src["id"]
    d = get_json(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings?limit=100")
    out = []
    for j in d.get("content", []):
        l = j.get("location") or {}
        loc = ", ".join(filter(None, [l.get("city"), l.get("region"), l.get("country")]))
        if l.get("remote"):
            loc += " | Remote"
        x = job(src, j["id"], j.get("name"), loc, f"https://jobs.smartrecruiters.com/{cid}/{j['id']}",
                j.get("releasedDate", ""))
        x["_raw"] = {"detail": f"https://api.smartrecruiters.com/v1/companies/{cid}/postings/{j['id']}"}
        out.append(x)
    return out


# ---------------------------------------------------------------- BambooHR
def bamboohr(src):
    sub = src["id"]
    d = get_json(f"https://{sub}.bamboohr.com/careers/list")
    out = []
    for j in d.get("result", []):
        l = j.get("location") or {}
        loc = ", ".join(filter(None, [l.get("city"), l.get("state"), l.get("addressCountry")]))
        if j.get("isRemote"):
            loc += " | Remote"
        x = job(src, j["id"], j.get("jobOpeningName"), loc, f"https://{sub}.bamboohr.com/careers/{j['id']}")
        x["_raw"] = {"detail": f"https://{sub}.bamboohr.com/careers/{j['id']}/detail"}
        out.append(x)
    return out


# ---------------------------------------------------------------- JazzHR (HTML)
def jazzhr(src):
    sub = src["id"]
    r = S.get(f"https://{sub}.applytojob.com/apply", timeout=TIMEOUT)
    r.raise_for_status()
    page = r.text
    out, seen = [], set()
    # job links look like https://{sub}.applytojob.com/apply/{shortId}/{Title-Slug}
    for m in re.finditer(r'href="((?:https?://[^"/]+)?/apply/([A-Za-z0-9]{6,})/[^"]*)"[^>]*>(.*?)</a>', page, re.S):
        code = m.group(2)
        if code in seen:
            continue
        seen.add(code)
        after = page[m.end(): m.end() + 800]
        nxt = re.search(r'/apply/[A-Za-z0-9]{6,}/', after)  # don't read into the next listing
        if nxt:
            after = after[: nxt.start()]
        txt = strip_html(after)
        lm = re.search(r"([A-Z][A-Za-z .'-]+, [A-Z]{2}(?:, [A-Za-z ]+)?|Remote)", txt)
        out.append(job(src, code, strip_html(m.group(3)), lm.group(1) if lm else "",
                       urljoin(f"https://{sub}.applytojob.com/", m.group(1))))
    return out


# ---------------------------------------------------------------- Workday
def _walk_facets(facets, parent=None):
    for f in facets or []:
        param = f.get("facetParameter", parent)
        for v in f.get("values", []) or []:
            if "values" in v:  # nested group
                yield from _walk_facets([v], param)
            elif v.get("id") and v.get("descriptor"):
                yield param, v["id"], v["descriptor"]


def workday(src, geo):
    tenant, n, site = src["tenant"], src["wd"], src["site"]
    host = f"https://{tenant}.wd{n}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    hdr = {"Content-Type": "application/json", "Accept": "application/json"}

    def post(body):
        r = S.post(api, json=body, headers=hdr, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    first = post({"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": src.get("search", "")})
    # pick location facet values that are in our area (city level), else Canada
    city_hits, country_hits = {}, {}
    for param, vid, desc in _walk_facets(first.get("facets")):
        d = desc.lower()
        if any(c in d for c in geo["include"]):
            city_hits.setdefault(param, []).append(vid)
        elif d.strip() in ("canada", "ca", "can") or d.endswith(", canada") and "ontario" in d:
            country_hits.setdefault(param, []).append(vid)
    applied = {}
    if city_hits:
        p = max(city_hits, key=lambda k: len(city_hits[k]))
        applied = {p: city_hits[p]}
    elif country_hits:
        p = max(country_hits, key=lambda k: len(country_hits[k]))
        applied = {p: country_hits[p]}
    filtered = bool(applied)
    if not applied:
        # no usable facet: fall back to text search for our region
        src = dict(src, search=(src.get("search", "") + " Ontario").strip())

    out, offset = [], 0
    while offset < 400:
        d = post({"appliedFacets": applied, "limit": 20, "offset": offset, "searchText": src.get("search", "")})
        posts = d.get("jobPostings", [])
        for j in posts:
            path = j.get("externalPath", "")
            jid = (j.get("bulletFields") or [path])[0]
            loc = j.get("locationsText", "")
            if filtered and re.match(r"^\d+ Locations?$", loc or ""):
                loc += " (in-area filter applied)"
            x = job(src, jid, j.get("title"), loc, f"{host}/{site}{path}", j.get("postedOn", ""))
            x["_raw"] = {"detail": f"{host}/wday/cxs/{tenant}/{site}{path}", "workday": True,
                         "area_filtered": filtered}
            out.append(x)
        offset += 20
        if len(posts) < 20 or offset >= d.get("total", 0):
            break
    return out


# ---------------------------------------------------------------- SuccessFactors RSS
def sf_rss(src):
    base = src["base"].rstrip("/")
    out = []
    for kw in src.get("keywords", ["engineer"]):
        r = S.get(f"{base}/services/rss/job/?locale=en_US&keywords={quote_plus(kw)}", timeout=TIMEOUT)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            loc = ""
            m = re.search(r"\(([^()]*)\)\s*$", title)  # "Title (City, ON, CA, M1M 1M1)"
            if m:
                loc, title = m.group(1), title[:m.start()].strip()
            jid = re.sub(r"\D", "", link.split("/")[-2] if link.endswith("/") else link.split("/")[-1]) or link
            out.append(job(src, jid, title, loc, link, it.findtext("pubDate") or "",
                           strip_html(it.findtext("description") or "")))
    return out


# ---------------------------------------------------------------- Jibe (AMD etc.)
def jibe(src):
    out = []
    for page in range(1, 11):
        d = get_json(f"{src['base'].rstrip('/')}/api/jobs", params=dict(src.get("params", {}), page=page))
        jobs = d.get("jobs", [])
        for w in jobs:
            j = w.get("data", w)
            loc = j.get("full_location") or ", ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")]))
            rid = j.get("req_id") or j.get("slug")
            url = f"{src['base'].rstrip('/')}/careers-home/jobs/{rid}" if rid else j.get("apply_url")
            out.append(job(src, j.get("req_id") or j.get("slug"), j.get("title"), loc, url,
                           j.get("posted_date", ""), strip_html(j.get("description", ""))))
        if not jobs or (d.get("totalCount") and page * len(jobs) >= d["totalCount"]):
            break
    return out


# ---------------------------------------------------------------- Eightfold (Qualcomm etc.)
def eightfold(src):
    out = []
    for start in range(0, 200, 10):
        d = get_json(f"{src['base'].rstrip('/')}/api/pcsx/search",
                     params=dict(src.get("params", {}), start=start))
        data = d.get("data", d)
        pos = data.get("positions") or []
        for j in pos:
            locs = j.get("locations") or ([j.get("location")] if j.get("location") else [])
            url = j.get("positionUrl") or j.get("canonicalPositionUrl") or ""
            if url.startswith("/"):
                url = src["base"].rstrip("/") + url
            posted = j.get("postedTs") or j.get("t_create") or ""
            if isinstance(posted, (int, float)):
                posted = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(posted))
            out.append(job(src, j.get("id") or j.get("displayJobId"), j.get("name") or j.get("title"),
                           " | ".join(map(str, locs)), url, posted,
                           strip_html(j.get("jobDescription") or j.get("job_description") or "")))
        if len(pos) < 10:
            break
    return out


# ---------------------------------------------------------------- Plain careers page
def pagelinks(src):
    """For companies with no job API: treat every link on the careers page as a
    possible posting. New links that look like job titles go through the filters."""
    r = S.get(src["url"], timeout=TIMEOUT)
    r.raise_for_status()
    out, seen = [], set()
    for href, text in re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', r.text, re.S | re.I):
        t = strip_html(text)
        if not (6 <= len(t) <= 120) or t.lower() in seen:
            continue
        seen.add(t.lower())
        url = urljoin(src["url"], html.unescape(href))
        out.append(job(src, re.sub(r"\W+", "-", t.lower())[:80], t, "", url))
    return out


# ---------------------------------------------------------------- Adzuna (aggregator, optional)
def adzuna(src, app_id, app_key):
    out = []
    for q in src.get("queries", []):
        d = get_json("https://api.adzuna.com/v1/api/jobs/ca/search/1", params={
            "app_id": app_id, "app_key": app_key, "what": q, "where": src.get("where", "Toronto"),
            "distance": src.get("distance_km", 60), "max_days_old": 3, "sort_by": "date",
            "results_per_page": 50, "content-type": "application/json"})
        for j in d.get("results", []):
            out.append(job(dict(src, name=(j.get("company") or {}).get("display_name", "Adzuna")),
                           j["id"], j.get("title"), (j.get("location") or {}).get("display_name", ""),
                           j.get("redirect_url"), j.get("created", ""), strip_html(j.get("description", ""))))
    return out


# ---------------------------------------------------------------- detail pages
def fetch_detail(j):
    """Fill in description for jobs whose list endpoint doesn't include it."""
    if j["description"] and len(j["description"]) > 200:
        return j
    det = j["_raw"].get("detail")
    try:
        if det and j["_raw"].get("workday"):
            d = get_json(det)
            info = d.get("jobPostingInfo", {})
            j["description"] = strip_html(info.get("jobDescription", ""))
            j["location"] = info.get("location", j["location"]) + (
                " | " + " | ".join(info.get("additionalLocations", [])) if info.get("additionalLocations") else "")
            j["posted"] = info.get("startDate") or j["posted"]
        elif det and "smartrecruiters" in det:
            d = get_json(det)
            secs = (d.get("jobAd") or {}).get("sections", {})
            j["description"] = "\n".join(strip_html(v.get("text", "")) for v in secs.values() if isinstance(v, dict))
        elif det and "bamboohr" in det:
            d = get_json(det)
            jo = (d.get("result") or {}).get("jobOpening", {})
            j["description"] = strip_html(jo.get("description", ""))
            j["posted"] = jo.get("datePosted") or j["posted"]
        elif j.get("url"):
            r = S.get(j["url"], timeout=TIMEOUT)
            r.raise_for_status()
            j["description"] = strip_html(r.text)[:20000]
    except Exception as e:  # description is nice-to-have; don't drop the job
        j["description"] = j["description"] or f"(could not load description: {e})"
    return j


FETCHERS = {
    "greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters,
    "bamboohr": bamboohr, "jazzhr": jazzhr, "workday": workday, "sf_rss": sf_rss, "jibe": jibe,
    "eightfold": eightfold, "pagelinks": pagelinks, "adzuna": adzuna,
}
