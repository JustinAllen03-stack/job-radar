#!/usr/bin/env python3
"""job-radar: polls employer job boards and alerts you to new matching postings.

  python radar.py               normal run (what GitHub Actions does every ~10 min)
  python radar.py --check       test every source, print counts, change nothing
  python radar.py --dry-run     full pipeline, print what WOULD be alerted, change nothing
  python radar.py --test-alert  send one test message to your phone
  python radar.py --rebuild-backlog   re-screen every open job with the current filters and rewrite BACKLOG.md
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


age_days = screening.age_days
posted_text = screening.posted_text


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


def decorate(j, cfg):
    j["_posted_text"] = posted_text(j.get("posted"))
    j["_loc_short"] = screening.area_location(j, cfg["geo"])
    return j


def screen(j, cfg, profile, use_claude=True, max_age=None):
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
    decorate(j, cfg)
    if a is not None and a > (max_age or cfg["limits"]["max_age_days"]):
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


BACKLOG_HEADER = ("# Open roles that match your filters\n\n"
                  "Newest first. Ages come from each job board; 'date not listed' means the board doesn't say.\n")


def _age_key(item):
    a = age_days(item[0].get("posted"))
    return (a is None, a if a is not None else 0, item[0]["company"])


def backlog_rows(items):
    return "\n".join(
        f"- [ ] **{j['company']}** - [{j['title']}]({j['url']}) - {j.get('_loc_short') or j['location']}"
        f" - posted {j.get('_posted_text') or posted_text(j.get('posted'))} - _{n}_"
        for j, n in sorted(items, key=_age_key))


def rebuild_backlog(results, cfg, profile, st, dry=False):
    """Re-screen every currently open job with today's filters and rewrite BACKLOG.md from scratch."""
    use_claude = bool(os.environ.get("ANTHROPIC_API_KEY"))
    budget = cfg["limits"].get("rebuild_detail_budget", 400)
    items, skipped = [], 0
    for s, jobs, err, dt in results:
        if err:
            print(f"ERROR {s['key']}: {err}")
            continue
        for j in jobs:
            st["seen"].setdefault(j["id"], NOW)
            if budget <= 0:
                skipped += 1
                continue
            if not (screening.location_ok(j, cfg["geo"])[0] and screening.title_ok(j, cfg)[0]):
                continue
            budget -= 1
            ok, note, v = screen(j, cfg, profile, use_claude=use_claude,
                                 max_age=cfg["limits"].get("backlog_max_age_days", 30))
            if ok:
                items.append((j, (f"Fit {v.get('fit')}/5: " if v else "") + note.replace("Claude: ", "")))
        if s["key"] not in st["seeded_sources"]:
            st["seeded_sources"].append(s["key"])
    text = BACKLOG_HEADER + (f"\n_Rebuilt {datetime.now().date()} with {'Claude + rules' if use_claude else 'rules only'}"
                             f"{f'; {skipped} jobs not checked (budget)' if skipped else ''}._\n\n") + backlog_rows(items) + "\n"
    print(text)
    if not dry:
        (ROOT / "BACKLOG.md").write_text(text)
        save_state(st)
        notify.send(text=f"job-radar: BACKLOG.md rebuilt, {len(items)} open roles match your filters.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-alert", action="store_true")
    ap.add_argument("--rebuild-backlog", action="store_true")
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

    if args.rebuild_backlog:
        return rebuild_backlog(results, cfg, profile, st, dry=args.dry_run)

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
                    ok, note, _ = screen(j, cfg, profile, use_claude=False,
                                         max_age=cfg["limits"].get("backlog_max_age_days", 30))
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
                                    j["title"], j["location"], j["url"], j.get("_posted_text", ""),
                                    (v or {}).get("reason", j.get("_rule_note", ""))])

    if backlog:
        path = ROOT / "BACKLOG.md"
        old = path.read_text() if path.exists() else BACKLOG_HEADER
        if args.dry_run:
            print("\n--- BACKLOG (dry run) ---\n" + backlog_rows(backlog))
        else:
            path.write_text(old + f"\n## Added {datetime.now().date()}\n{backlog_rows(backlog)}\n")
            notify.send(text=f"job-radar: {len(backlog)} currently-open roles from newly added companies match your "
                             f"filters; see BACKLOG.md in your repo.")

    if not args.dry_run:
        save_state(st)


if __name__ == "__main__":
    main()
