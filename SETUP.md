# job-radar setup (about 30 minutes)

What it does: every ~10 minutes, GitHub's servers check ~46 GTA/Waterloo medtech and PCB/hardware employers' job boards
directly (not LinkedIn/Indeed, which lag and block bots). New postings go through 3 filters:

1. Location (GTA + Waterloo region, or remote-Canada) and title (engineering roles; drops senior/lead/manager/intern/co-op/sales/pure software/chip RTL).
2. Rules on the description: drops "3+ years", "minimum five years", "Master's/PhD required", "P.Eng required" (EIT is fine).
3. Claude (optional, ~cents/month) reads what's left next to your profile and gives a fit score + one-line reason.

Survivors are sent to your phone. On the first run nothing is sent per-job; roles that are already open go into `BACKLOG.md` so you can apply to those too.

---

## 1. Put the project on GitHub (public repo = free unlimited runs)

Open Terminal and run:

```bash
cd ~/code/job-radar
git init && git add . && git commit -m "job-radar"
gh repo create job-radar --public --source=. --push
```

No `gh`? Install it with `brew install gh`, then `gh auth login`. Or create an empty public repo named `job-radar` on github.com and follow its "push an existing repository" commands.

Why public: private repos get 2,000 free Actions minutes/month; a run every 10 min uses ~4,300. Nothing secret is in the code
(keys live in GitHub Secrets). The repo does show your profile summary (`profile.txt`, no phone/email) and the company list.
If you want it private, change `*/10` to `*/30` in `.github/workflows/radar.yml`.

## 2. Phone alerts

Set up **one WhatsApp option + ntfy as a backup**. Both WhatsApp routes have catches (below), so the backup is worth the 2 minutes.

### Option A: CallMeBot WhatsApp (free, simplest), *if it has space*
As of Oct 2026 its page says "The bot is currently full. Please check back in a few days."
1. Go to https://www.callmebot.com/blog/free-api-whatsapp-messages/ and check whether the bot number is visible.
2. Save that number in your contacts, then WhatsApp it: `I allow callmebot to send me messages`
3. You get back an API key. Secrets to add: `CALLMEBOT_PHONE` (e.g. `+16475550000`) and `CALLMEBOT_APIKEY`.

### Option B: Meta WhatsApp Cloud API (official, more setup)
1. developers.facebook.com → My Apps → Create app → type "Business" → add the **WhatsApp** product.
2. In WhatsApp → API Setup: note the **Phone number ID**, and add + verify your own number as a recipient.
3. WhatsApp Manager → Message templates → Create: name `job_alert`, category **Utility**, language English, body exactly:
   `New job: {{1}} at {{2}} ({{3}}). {{4}} Apply: {{5}}` — wait for approval (often minutes, can be longer).
4. Business Settings → System users → add one, give it the app with `whatsapp_business_messaging`, generate a **permanent** token
   (the token on the API Setup page expires in 24 h).
5. Secrets: `WA_TOKEN`, `WA_PHONE_ID`, `WA_TO` (your number, digits only, e.g. `16475550000`), `WA_TEMPLATE` = `job_alert`.
Meta may charge a small per-message fee for template messages; check their current pricing page. Meta's menus change often, so these clicks may be slightly off.

(Twilio's WhatsApp sandbox is not used: you'd have to re-join it every 3 days.)

### Backup: ntfy push (free, no account)
1. Install the **ntfy** app (iOS/Android), tap +, subscribe to a hard-to-guess topic, e.g. `justin-radar-8f3k2q`.
2. Secret: `NTFY_TOPIC` = that topic name. Anyone who knows the topic can read it, so don't use something guessable.

## 3. Add the secrets

GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**. Add each one from step 2, plus:

- `ANTHROPIC_API_KEY` — from console.anthropic.com → API keys. Optional; without it, only the free rules run (more false alerts).
- `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` — optional catch-all across many job sites (developer.adzuna.com, free signup).
  Slower (hours, not minutes) but catches companies not on the list. Assumption: Adzuna's API covers Canada (adzuna.ca exists); `--check` will show if it doesn't.

No `.env` file needed. GitHub passes these to the script.

## 4. Turn it on and test

Repo → **Actions** tab → enable workflows if asked → **job-radar** → **Run workflow**, and choose the mode:

1. `test-alert` → you should get a test message within a minute. If not, open the run log; it says which channel failed.
2. `check` → log lists every company: jobs found, how many pass location+title, errors. **Expect a few sources to error**
   (I couldn't reach the boards from my workspace to live-test, see below). Delete or fix any line in `config.yaml` that keeps failing.
3. `run` → first real run: seeds every source, writes `BACKLOG.md`, sends one "job-radar is live" message.

After that it runs on its own every ~10 minutes. If a company's board breaks for ~3 hours straight, you get one warning message.

## 5. Tuning (edit `config.yaml` on github.com, pencil icon)

- Too many alerts → raise `claude.min_fit` to 4, or add words to `titles.exclude`.
- Missing roles you'd want → add words to `titles.role_terms` / `hardware_terms`, or remove from `exclude`.
- Add a company: copy a line of the same type. Find the board type by opening their careers page and looking at the job link:
  `boards.greenhouse.io/NAME` → greenhouse, `jobs.lever.co/NAME` → lever, `jobs.ashbyhq.com/NAME` → ashby,
  `NAME.bamboohr.com` → bamboohr, `NAME.applytojob.com` → jazzhr, `TENANT.wdN.myworkdayjobs.com/SITE` → workday.
  Anything else → `type: pagelinks` with the careers page URL.
- Every alert is logged in `state/alerts.csv`.

## Honest limits

- **Not "the second it's posted."** Worst case ≈ 10 min schedule + GitHub delays (usually a few min, sometimes 15+ at busy times).
  Still well ahead of LinkedIn/Indeed, which re-post hours to days later.
- **Not live-tested.** My workspace's network blocks these job sites, so the parsers are built from each board's documented/observed
  JSON and unit-tested on sample responses (77 tests pass). Unverified feeds: SuccessFactors RSS (NDI, ATS, Olympus, Celestica), SmartRecruiters (Cloud DX),
  JazzHR HTML (Intellijoint, Myant). Run `check` first.
- **Coverage gaps** (no pollable board found, check manually or via LinkedIn alerts): Synaptive Medical (Dayforce), Boston Scientific
  Mississauga (Eightfold), Siemens Healthineers, Christie, Honeywell, MDA Space, Xanadu, Nanoleaf, Nuvation, KA Imaging, Theralase.
- Big Workday employers (Medtronic, J&J, Philips, GE, Abbott, BD) mostly post sales/clinical roles in the GTA; expect few hits there.
- Filters will occasionally be wrong both ways. When a role says "3-5 years preferred", it can still be worth applying.
