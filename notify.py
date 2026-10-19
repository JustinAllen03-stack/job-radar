"""Alert channels. Every channel whose secrets are set gets the message.

WhatsApp options:
  1. CallMeBot (free, personal use)   -> CALLMEBOT_PHONE, CALLMEBOT_APIKEY
  2. Meta WhatsApp Cloud API          -> WA_TOKEN, WA_PHONE_ID, WA_TO, WA_TEMPLATE
Backup (recommended, free, very reliable):
  3. ntfy push notification           -> NTFY_TOPIC
"""
import os
import re

import requests


def _clean(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s[: n - 1] + "…" if len(s) > n else s


def format_text(j, verdict):
    lines = [f"NEW JOB: {j['title']}",
             f"{j['company']} | {j.get('_loc_short') or j['location'] or 'location not listed'}",
             f"Posted: {j.get('_posted_text') or 'date not listed'}"]
    if verdict:
        lines.append(f"Fit {verdict.get('fit', '?')}/5: {verdict.get('reason', '')}")
    elif j.get("_rule_note"):
        lines.append(j["_rule_note"])
    lines.append(f"Apply: {j['url']}")
    return "\n".join(lines)


def send(j=None, verdict=None, text=None):
    text = text or format_text(j, verdict)
    sent, errors = [], []
    env = os.environ

    if env.get("CALLMEBOT_PHONE") and env.get("CALLMEBOT_APIKEY"):
        try:
            r = requests.get("https://api.callmebot.com/whatsapp.php", timeout=30, params={
                "phone": env["CALLMEBOT_PHONE"], "text": text, "apikey": env["CALLMEBOT_APIKEY"]})
            r.raise_for_status()
            sent.append("callmebot")
        except Exception as e:
            errors.append(f"callmebot: {e}")

    if env.get("WA_TOKEN") and env.get("WA_PHONE_ID") and env.get("WA_TO"):
        try:
            tpl = env.get("WA_TEMPLATE", "job_alert")
            # template body must be: New job: {{1}} at {{2}} ({{3}}). {{4}} Apply: {{5}}
            if j:
                params = [_clean(j["title"], 120), _clean(j["company"], 60), _clean((j.get("_loc_short") or j["location"] or "n/a") + f", posted {j.get('_posted_text', '?')}", 80),
                          _clean((verdict or {}).get("reason") or j.get("_rule_note", "") or "-", 150),
                          _clean(j["url"], 500)]
            else:
                params = [_clean(text, 120), "job-radar", "-", "-", "-"]
            r = requests.post(f"https://graph.facebook.com/v21.0/{env['WA_PHONE_ID']}/messages", timeout=30,
                              headers={"Authorization": f"Bearer {env['WA_TOKEN']}"},
                              json={"messaging_product": "whatsapp", "to": env["WA_TO"], "type": "template",
                                    "template": {"name": tpl, "language": {"code": env.get("WA_LANG", "en")},
                                                 "components": [{"type": "body", "parameters": [
                                                     {"type": "text", "text": p} for p in params]}]}})
            r.raise_for_status()
            sent.append("whatsapp-cloud")
        except Exception as e:
            errors.append(f"whatsapp-cloud: {e} {getattr(getattr(e, 'response', None), 'text', '')[:300]}")

    if env.get("NTFY_TOPIC"):
        try:
            hdr = {"Title": _clean(f"{j['title']} - {j['company']}" if j else "job-radar", 120).encode("utf-8"),
                   "Priority": "high", "Tags": "briefcase"}
            if j:
                hdr["Click"] = j["url"]
            r = requests.post(f"https://ntfy.sh/{env['NTFY_TOPIC']}", data=text.encode("utf-8"),
                              headers=hdr, timeout=30)
            r.raise_for_status()
            sent.append("ntfy")
        except Exception as e:
            errors.append(f"ntfy: {e}")

    return sent, errors
