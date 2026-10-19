#!/usr/bin/env python3
"""job-radar: polls employer job boards and alerts you to new matching postings.

  python radar.py               normal run (what GitHub Actions does every ~10 min)
  python radar.py --check       test every source, print counts, change nothing
  python radar.py --dry-run     full pipeline, print what WOULD be alerted, change nothing
  python radar.py --test-alert  send one test message to your phone
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import yaml

import notify
import screening
import sources

ROOT = Path(__file__).parent
STATE = ROOT / "state" / "seen.json"
NOW = time.time()


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"seen": {}, "seeded_sources": [], "health": {}, "alerted_keys": {}}


def save_state(st):
    cutoff = NOW - 150 * 86400
    st["seen"] = {k: v for k, v in st["seen"].items() if v > cutoff}
    st["alerted_keys"] = {k: v for k, v in st["alerted_keys"].items() if v > NOW - 45 * 86400}
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, indent=0, sort_keys=True))


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


def build_sources(cfg):
    out = []
    for cat, lst in cfg["companies"].items():
        for s in lst or []:
            s = dict(s, category=s.get("category", cat))
            s["key"] = s.get("key") or re.sub(r"\W+", "-", f"{s['type']}-{s['name']}".lower())
            out.append(s)
    if os.environ.get("ADZUNA_APP_ID") and cfg.get("adzuna", {}).get("enabled", True):
        out.append(dict(cfg["adzuna"], type="adzuna", name="Adzuna", key="adzuna", category="aggregator"))
    return out


def fetch_one(s, geo):
    t0 = time.time()
    try:
        if s["type"] == "workday":
            jobs = sources.workday(s, geo)
        elif s["type"] == "adzuna":
            jobs = sources.adzuna(s, os.environ["ADZUNA_APP_ID"], os.environ["ADZUNA_APP_KEY"])
        else:
            jobs = sources.FETCHERS[s["type"]](s)
        return s, jobs, None, time.time() - t0
    except Exception as e:
        return s, [], f"{type(e).__name__}: {str(e)[:200]}", time.time() - t0


def screen(j, cfg, profile, use_claude=True):
    """Returns (passed, note, verdict)."""
    ok, why = screening.location_ok(j, cfg["geo"])
    if not ok:
        return False, f"location: {why}", None
    ok, why = screening.title_ok(j, cfg)
    if not ok:
        return False, why, None
    sources.fetch_detail(j)
    # re-check location (Workday detail gives the real city)
    ok, why = screening.location_ok(j, cfg["geo"])
    if not ok:
        return False, f"location: {why}", None
    a = age_days(j["posted"])
    if a is not None and a > cfg["limits"]["max_age_days"]:
        return False, f"old posting ({int(a)} days)", None
    ok, why, _ = screening.rules_check(j, cfg)
    if not ok:
        return False, why, None
    j["_rule_note"] = f"Rules: {why}"
    if use_claude:
        try:
            v = screening.claude_check(j, cfg, profile)
        except Exception as e:
            print(f"   ! Claude check failed ({e}); falling back to rules")
            v = None
        if v is not None:
            return v["apply"], f"Claude: {v.get('reason')}", v
    return True, j["_rule_note"], None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-alert", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    profile = (ROOT / "profile.txt").read_text()

    if args.test_alert:
        sent, errs = notify.send(text="job-radar test: alerts are working. Next alerts will be real postings.")
        print("sent via:", sent or "NOTHING", "\nerrors:", errs)
        sys.exit(0 if sent else 1)

    srcs = build_sources(cfg)
    st = load_state()
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(lambda s: fetch_one(s, cfg["geo"]), srcs))

    if args.check:
        print(f"{'source':45} {'jobs':>5} {'in-area+title':>13}  time  status")
        for s, jobs, err, dt in results:
            keep = [j for j in jobs if screening.location_ok(j, cfg["geo"])[0] and screening.title_ok(j, cfg)[0]]
            print(f"{s['key'][:45]:45} {len(jobs):5} {len(keep):13} {dt:5.1f}s  {err or 'ok'}")
            for j in keep[:3]:
                print(f"{'':8}- {j['title']} | {j['location']}")
        return

    alerts, backlog, log = [], [], []
    fail_alert_at = cfg["limits"].get("alert_after_failures", 18)
    detail_budget = cfg["limits"].get("seed_detail_budget", 150)

    for s, jobs, err, dt in results:
        h = st["health"].setdefault(s["key"], {"fails": 0})
        if err:
            h["fails"] += 1
            log.append(f"ERROR {s['key']}: {err}")
            if h["fails"] == fail_alert_at and not args.dry_run:
                notify.send(text=f"job-radar: source '{s['name']}' has failed {fail_alert_at} runs in a row. "
                                 f"Last error: {err[:150]}. Check config.yaml.")
            continue
        h.update(fails=0, last_ok=int(NOW), count=len(jobs))
        seeding = s["key"] not in st["seeded_sources"]
        new = [j for j in jobs if j["id"] not in st["seen"]]
        for j in new:
            if seeding:
                # first time we see this source: record everything, alert nothing,
                # but list current matches in BACKLOG.md so you can apply to them too
                if detail_budget > 0 and screening.location_ok(j, cfg["geo"])[0] and screening.title_ok(j, cfg)[0]:
                    detail_budget -= 1
                    ok, note, _ = screen(j, cfg, profile, use_claude=False)
                    if ok:
                        backlog.append((j, note))
            else:
                ok, note, v = screen(j, cfg, profile)
                log.append(f"{'MATCH' if ok else 'skip '} {j['company']} | {j['title']} | {j['location']} -> {note}")
                if ok:
                    dedupe = re.sub(r"\W+", " ", f"{j['company']}|{j['title']}".lower())
                    if dedupe in st["alerted_keys"]:
                        log.append("      (already alerted from another source)")
                    else:
                        alerts.append((j, v))
                        st["alerted_keys"][dedupe] = NOW
            st["seen"][j["id"]] = NOW
        if seeding:
            st["seeded_sources"].append(s["key"])
            log.append(f"SEEDED {s['key']}: {len(jobs)} existing jobs recorded silently")

    print("\n".join(log) or "no new postings")
    print(f"\n{len(alerts)} alert(s), {len(backlog)} backlog match(es)")

    for j, v in alerts:
        if args.dry_run:
            print("\n--- WOULD ALERT ---\n" + notify.format_text(j, v))
            continue
        sent, errs = notify.send(j, v)
        print(f"alerted {j['title']} via {sent} {errs or ''}")
        with open(ROOT / "state" / "alerts.csv", "a", newline="") as f:
            csv.writer(f).writerow([datetime.now(timezone.utc).isoformat(timespec="minutes"), j["company"],
                                    j["title"], j["location"], j["url"], (v or {}).get("reason", j.get("_rule_note", ""))])

    if backlog:
        path = ROOT / "BACKLOG.md"
        old = path.read_text() if path.exists() else "# Open roles found when sources were first added\n\nRules-only screen (no Claude). Check each before applying.\n"
        rows = "\n".join(f"- [ ] **{j['company']}** - [{j['title']}]({j['url']}) - {j['location']} - _{n}_"
                         for j, n in sorted(backlog, key=lambda x: (x[0]['company'], x[0]['title'])))
        if args.dry_run:
            print("\n--- BACKLOG (dry run) ---\n" + rows)
        else:
            path.write_text(old + f"\n## Added {datetime.now().date()}\n{rows}\n")
            notify.send(text=f"job-radar is live. {len(backlog)} currently-open roles match your filters; "
                             f"see BACKLOG.md in your repo. From now on you'll get one message per new posting.")

    if not args.dry_run:
        save_state(st)


if __name__ == "__main__":
    main()
