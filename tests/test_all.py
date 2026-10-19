import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
import radar  # noqa: E402
import screening  # noqa: E402
import sources  # noqa: E402

CFG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text())
GEO = CFG["geo"]


def J(title, loc="Toronto, ON", cat="medtech", desc="", local=False):
    return {"id": "x", "company": "Co", "category": cat, "local_company": local, "title": title,
            "location": loc, "url": "u", "posted": "", "description": desc, "_raw": {}}


# ---------------- location
@pytest.mark.parametrize("loc,ok", [
    ("Toronto, Ontario, Canada", True), ("Mississauga, ON", True), ("Waterloo, ON", True),
    ("Remote - Canada", True), ("Ottawa, Ontario", False), ("Kanata, ON", False),
    ("San Jose, CA, United States", False), ("Remote - US", False), ("Ontario, Canada", True),
    ("Markham | Remote", True), ("Vancouver, BC", False), ("London, Ontario", False),
    ("Bangalore, India", False), ("3 Locations (in-area filter applied)", True),
])
def test_location(loc, ok):
    assert screening.location_ok(J("Engineer", loc), GEO)[0] is ok


def test_unknown_location_local_company():
    assert screening.location_ok(J("Engineer", "", local=True), GEO)[0]
    assert not screening.location_ok(J("Engineer", "", local=False), GEO)[0]


# ---------------- titles
@pytest.mark.parametrize("title,cat,ok", [
    ("R&D Engineer", "medtech", True),
    ("Quality Engineer I", "medtech", True),
    ("Biomedical Engineer", "medtech", True),
    ("Senior Hardware Engineer", "hardware", False),
    ("Sr. Electrical Engineer", "hardware", False),
    ("Hardware Engineer", "hardware", True),
    ("PCB Design Engineer", "hardware", True),
    ("Junior Electronics Designer", "hardware", True),
    ("Software Engineer", "hardware", False),
    ("Software Engineer", "medtech", False),
    ("Embedded Software Engineer", "medtech", True),
    ("ASIC Design Verification Engineer", "hardware", False),
    ("RTL Design Engineer", "hardware", False),
    ("Hardware Engineering Intern", "hardware", False),
    ("Engineering Co-op (Winter 2027)", "medtech", False),
    ("Account Manager", "medtech", False),
    ("Manufacturing Engineer III", "medtech", False),
    ("Manufacturing Engineer II", "medtech", True),
    ("Associate Project Manager", "medtech", True),
    ("Project Manager", "medtech", False),
    ("Sales Engineer", "hardware", False),
    ("Hardware Test Engineer", "hardware", True),
    ("Signal Integrity Engineer", "hardware", True),
    ("Data Scientist", "medtech", False),
    ("Field Service Engineer", "medtech", True),
    ("Contact us", "medtech", False),
])
def test_titles(title, cat, ok):
    assert screening.title_ok(J(title, cat=cat), CFG)[0] is ok, screening.title_ok(J(title, cat=cat), CFG)


# ---------------- experience / degree rules
@pytest.mark.parametrize("desc,ok", [
    ("Requirements: Bachelor's in Electrical Engineering. 0-2 years of experience.", True),
    ("Minimum 3 years of experience in PCB design.", False),
    ("3+ years of hardware design experience", False),
    ("You have 5-7 years experience with Altium.", False),
    ("At least five years of relevant experience required.", False),
    ("1-3 years of experience in a regulated industry", True),
    ("2+ years experience preferred", True),
    ("3+ years of experience is an asset", True),
    ("New grads welcome! Bachelor's degree in Biomedical Engineering.", True),
    ("Master's degree in Electrical Engineering required.", False),
    ("Bachelor's or Master's degree required in engineering", True),
    ("PhD required in physics or related field", False),
    ("Master's degree preferred.", True),
    ("Our company has 25 years of history. 1+ years of experience.", True),
    ("Must be a licensed Professional Engineer (P.Eng) in Ontario.", False),
    ("Must be a licensed P.Eng or EIT in Ontario.", True),
    ("4-year degree in engineering; 2 years of experience", True),
])
def test_rules(desc, ok):
    assert screening.rules_check(J("Engineer", desc=desc), CFG)[0] is ok, screening.rules_check(J("Engineer", desc=desc), CFG)


# ---------------- age parsing
@pytest.mark.parametrize("p,lo,hi", [
    ("Posted Today", 0, 0), ("Posted Yesterday", 1, 1), ("Posted 3 Days Ago", 3, 3),
    ("Posted 30+ Days Ago", 30, 30), ("2020-01-01T00:00:00Z", 365, 99999),
    ("Mon, 01 Jan 2024 10:00:00 GMT", 365, 99999),
])
def test_age(p, lo, hi):
    a = radar.age_days(p)
    assert lo <= a <= hi


# ---------------- parsers against canned API responses
class Resp:
    def __init__(self, data=None, text="", content=b""):
        self._d, self.text, self.content = data, text, content or text.encode()

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


def test_greenhouse(monkeypatch):
    monkeypatch.setattr(sources.S, "get", lambda *a, **k: Resp({"jobs": [{
        "id": 1, "title": "Hardware Engineer", "location": {"name": "Toronto"},
        "absolute_url": "https://x/1", "updated_at": "2026-10-17", "content": "&lt;p&gt;2 years experience&lt;/p&gt;"}]}))
    j = sources.greenhouse({"key": "gh", "name": "X", "id": "x"})[0]
    assert j["title"] == "Hardware Engineer" and j["description"] == "2 years experience"


def test_lever(monkeypatch):
    monkeypatch.setattr(sources.S, "get", lambda *a, **k: Resp([{
        "id": "a", "text": "PCB Engineer", "categories": {"location": "Toronto"}, "hostedUrl": "u",
        "createdAt": 1760000000000, "descriptionPlain": "desc", "lists": [{"text": "Reqs", "content": "<li>3+ years</li>"}]}]))
    j = sources.lever({"key": "lv", "name": "X", "id": "x"})[0]
    assert "3+ years" in j["description"] and j["posted"].startswith("2025")


def test_ashby(monkeypatch):
    monkeypatch.setattr(sources.S, "get", lambda *a, **k: Resp({"jobs": [{
        "id": "b", "title": "Hardware Engineer", "location": "Toronto", "isRemote": False,
        "jobUrl": "u", "publishedAt": "2026-10-17T00:00:00Z", "descriptionPlain": "d"}]}))
    assert sources.ashby({"key": "a", "name": "X", "id": "x"})[0]["location"] == "Toronto"


def test_bamboohr(monkeypatch):
    monkeypatch.setattr(sources.S, "get", lambda *a, **k: Resp({"result": [{
        "id": "7", "jobOpeningName": "Electrical Engineer", "location": {"city": "Waterloo", "state": "Ontario"}}]}))
    j = sources.bamboohr({"key": "b", "name": "X", "id": "voltera"})[0]
    assert j["url"].endswith("/careers/7") and "Waterloo" in j["location"]


def test_jazzhr(monkeypatch):
    page = '''<ul><li class="list-group-item"><h4 class="list-group-item-heading">
    <a href="https://myant.applytojob.com/apply/AbC123/RD-Electrical-Engineer">R&amp;D Electrical Engineer</a></h4>
    <ul class="list-inline list-group-item-text"><li><i class="fa fa-map-marker"></i>Mississauga, ON</li></ul></li></ul>'''
    monkeypatch.setattr(sources.S, "get", lambda *a, **k: Resp(text=page))
    j = sources.jazzhr({"key": "j", "name": "Myant", "id": "myant"})[0]
    assert j["title"] == "R&D Electrical Engineer" and j["location"] == "Mississauga, ON"


def test_workday_facets_and_paging(monkeypatch):
    calls = []
    facets = [{"facetParameter": "locationMainGroup", "values": [{"facetParameter": "locations", "values": [
        {"id": "L1", "descriptor": "Mississauga, Ontario", "count": 3},
        {"id": "L2", "descriptor": "Minneapolis, MN", "count": 90}]}]}]

    def post(url, json=None, **k):
        calls.append(json)
        if not json["appliedFacets"]:
            return Resp({"total": 500, "facets": facets, "jobPostings": []})
        return Resp({"total": 1, "jobPostings": [{"title": "R&D Engineer", "externalPath": "/job/Mississauga/RD_R1",
                                                   "locationsText": "Mississauga, Ontario", "postedOn": "Posted Today",
                                                   "bulletFields": ["R1"]}]})
    monkeypatch.setattr(sources.S, "post", post)
    js = sources.workday({"key": "wd", "name": "Medtronic", "tenant": "medtronic", "wd": 1, "site": "MC"}, GEO)
    assert calls[1]["appliedFacets"] == {"locations": ["L1"]}
    assert js[0]["url"] == "https://medtronic.wd1.myworkdayjobs.com/MC/job/Mississauga/RD_R1"


def test_sf_rss(monkeypatch):
    xml = b'''<rss><channel><item><title>Hardware Design Engineer (Toronto, ON, CA, M3C 1V7)</title>
    <link>https://careers.celestica.com/job/Toronto-Hardware/123456/</link><pubDate>Fri, 16 Oct 2026 10:00:00 GMT</pubDate>
    <description>&lt;p&gt;1-2 years experience&lt;/p&gt;</description></item></channel></rss>'''
    monkeypatch.setattr(sources.S, "get", lambda *a, **k: Resp(content=xml, text=xml.decode()))
    j = sources.sf_rss({"key": "c", "name": "Celestica", "base": "https://careers.celestica.com", "keywords": ["x"]})[0]
    assert j["title"] == "Hardware Design Engineer" and j["location"].startswith("Toronto") and j["id"].endswith("123456")


def test_pipeline_end_to_end(monkeypatch):
    """New job on an already-seeded source: passes filters -> alert; senior job -> skipped."""
    j_ok = J("Hardware Test Engineer", "Toronto, ON", "hardware", "Bachelor's in EE. 1+ years experience.")
    j_bad = J("Hardware Test Engineer", "Toronto, ON", "hardware", "Minimum 5 years experience required.")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert radar.screen(j_ok, CFG, "profile")[0]
    assert not radar.screen(j_bad, CFG, "profile")[0]


def test_claude_verdict_overrides(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    class R:
        def raise_for_status(self): pass
        def json(self): return {"content": [{"type": "text", "text": json.dumps(
            {"apply": True, "min_years_required": 4, "degree_required": "bachelor", "fit": 5, "reason": "x"})}]}
    monkeypatch.setattr(screening.requests, "post", lambda *a, **k: R())
    v = screening.claude_check(J("Engineer", desc="..."), CFG, "p")
    assert v["apply"] is False  # 4 years > max 2, even though model said apply


def test_jazzhr_plain_layout(monkeypatch):
    page = '''<div><h3><a href="/apply/gwM9w28c7P/Junior-Electrical-Engineer">Junior Electrical Engineer</a></h3>
    <span>Mississauga, ON, Canada</span></div><div><h3><a href="https://myant.applytojob.com/apply/zzZZ99yy11/QA">QA Lead</a></h3>
    <span>Remote</span></div>'''
    monkeypatch.setattr(sources.S, "get", lambda *a, **k: Resp(text=page))
    js = sources.jazzhr({"key": "j", "name": "Myant", "id": "myant"})
    assert js[0]["location"].startswith("Mississauga, ON") and js[0]["url"].startswith("https://myant.applytojob.com/apply/")
    assert js[1]["location"] == "Remote"


def test_amd_jibe(monkeypatch):
    monkeypatch.setattr(sources.S, "get", lambda *a, **k: Resp({"jobs": [{"data": {
        "title": "Hardware Design Engineer", "req_id": "93427", "full_location": "MARKHAM, Canada",
        "posted_date": "2026-10-15T15:39:00+0000", "description": "1 year experience",
        "apply_url": "https://canadacareers-amd.icims.com/jobs/93427/login"}}]} if k.get("params", {}).get("page") == 1 else {"jobs": []}))
    js = sources.jibe({"key": "amd", "name": "AMD", "base": "https://careers.amd.com", "params": {"location": "Markham"}})
    assert len(js) == 1 and js[0]["url"] == "https://careers.amd.com/careers-home/jobs/93427"
    assert screening.location_ok(js[0], GEO)[0]
    assert radar.age_days(js[0]["posted"]) is not None


def test_qualcomm_eightfold(monkeypatch):
    monkeypatch.setattr(sources.S, "get", lambda *a, **k: Resp({"data": {"count": 1, "positions": [{
        "id": 446719716587, "name": "Hardware Engineer", "locations": ["Markham, Ontario, Canada"],
        "postedTs": 1789603200, "positionUrl": "/careers/job/446719716587"}]}}))
    j = sources.eightfold({"key": "q", "name": "Qualcomm", "base": "https://careers.qualcomm.com", "params": {}})[0]
    assert j["url"] == "https://careers.qualcomm.com/careers/job/446719716587" and j["posted"].startswith("2026-09")


def test_bamboohr_detail(monkeypatch):
    monkeypatch.setattr(sources.S, "get", lambda *a, **k: Resp({"result": {"jobOpening": {
        "description": "<p>2 years experience</p>", "datePosted": "2026-10-02"}}}))
    j = J("Electrical Engineer")
    j["_raw"] = {"detail": "https://voltera.bamboohr.com/careers/137/detail"}
    sources.fetch_detail(j)
    assert j["description"] == "2 years experience" and j["posted"] == "2026-10-02"
