---
name: job-search-cloud-agent
description: Sets up a fully autonomous, cloud-hosted daily job-search-and-outreach agent for whoever is running this outreachos repo — it keeps hunting for matching roles AND promising early-stage startups (not just Indeed/LinkedIn job boards), filters them through this repo's pipeline.py rules, writes personalized outreach drafts, and leaves them as Gmail drafts every day, entirely independent of whether the user's computer is on. Use this skill whenever the user wants their job search automated, says things like "make this work even when my PC is off", wants a daily/recurring scheduled job search, wants to move this project "to the cloud", wants to broaden discovery beyond job boards to startups/YC/accelerators, or wants a cloud agent that finds candidates and drafts outreach emails on its own each day. Also trigger if the user asks to set up or reconfigure the existing "outreachos-gunluk-tarama"-style routine, or asks why their scheduled search only runs when their machine is on.
---

# Job search cloud agent

Turns this repo's outreach pipeline (`src/pipeline.py`, `src/ai.py`) — which today only runs when a human (or Claude Code session) is actively driving it — into a **cloud-hosted scheduled agent** that runs on its own every day, whether or not the user's computer is on. This is the setup workflow used to bootstrap https://github.com/tarikdeveci/outreachos's own daily routine; it's written to work for anyone running their own fork with their own profile.

## What gets created

1. A private Google Drive folder holding the user's `state.json` (profile + role filters + exclusion rules + contact history) and `outreach_log.csv` — this is the persistent memory the cloud agent reads/writes each run.
2. A scheduled cloud routine (via the `schedule` skill / `RemoteTrigger` tool) that, every day:
   - Checks out this repo's code (public is fine — it holds no secrets, just the decision engine)
   - Reads the user's rules fresh from `src/pipeline.py` and profile from Drive
   - Searches for matching roles **and** promising early-stage startups — see "Broadening discovery" below, this is the part manual job-board searches miss
   - Filters candidates through the zero-tolerance rules (never softened, never overridden)
   - Writes the outreach email itself (same voice/constraints as `src/ai.py`'s `DRAFT_SYSTEM`) and creates it as a Gmail draft — **never sends**
   - Writes back updated state + a daily summary to Drive

## Before you start: gather what you need

Don't guess any of this — ask the user or read it from the repo:

1. **Profile/rules.** Does `state.json` exist locally (gitignored, so check the filesystem, not git)? If not, help the user fill one in from `state.example.json` — name, role filters (`seniority_exclude_keywords`, `title_exclude_keywords_hard`, `role_categories_include`), `excluded_sectors`, `excluded_companies_seed_personal` (people they know — this list matters, nobody wants an outreach agent cold-emailing their friend's company). Read `src/pipeline.py`'s `role_filter`/`sector_filter` to understand exactly which fields it consumes before asking — don't invent fields it doesn't read.
2. **Code repo URL.** `git remote -v` in this directory. It needs to be reachable by the cloud environment — public is simplest since the code has no secrets in it (check `.gitignore` to confirm personal files stay out).
3. **Connectors.** The routine needs a way to persist state and create drafts without the user's local OAuth tokens. Don't assume — call `RemoteTrigger` with a throwaway `{action: "create", enabled: false, ...}` test body first (see the `schedule` skill for the shape) and read the `mcp_connections` array in the response: it lists every connector already available to routines on this account (commonly includes Gmail and Google Drive if the user has ever used them in Claude). If Gmail or Drive is missing, tell the user to connect it at https://claude.ai/customize/connectors before continuing — don't silently fall back to something weaker.

## Known pitfall: don't use a private GitHub repo as the data store

The first instinct is "put `state.json` in a private repo, give the routine both repos as sources." **This reliably fails** with `403: You don't have access to a repository this routine uses`, even right after the user confirms the repo shows as connected in their GitHub/Claude settings. The reason: Claude Code's cloud routines authenticate to GitHub through an OAuth App, and OAuth Apps get access via **scope**, not per-repo selection — if it was granted at `public_repo` scope (typical, since most routines only need public code), private repos are invisible to it no matter what the user toggles in GitHub's UI, because there's nothing to toggle for an OAuth App.

Skip straight to **Google Drive** for anything private (state, history, resumes). It's connector-based (not git-scope-based), already works if the user has ever connected Drive to Claude, and needs no secret-token juggling. Public repo = code. Drive = data. Don't relitigate this by trying the private-repo route again unless GitHub App-based (not OAuth App-based) access becomes available — check by testing with a throwaway public-repo-only routine first (see prerequisite 3) before attempting anything with two repo sources.

## Seeding the Drive folder

Create a folder (any Drive `create_file` call with `mimeType: application/vnd.google-apps.folder`) and upload the user's `state.json` and `outreach_log.csv` as plain text (`disableConversionToGoogleType: true`, matching mime type) so they stay readable JSON/CSV rather than becoming Google Docs. Do **not** bother uploading CV PDFs — `src/ai.py`'s draft generation only ever reads the `profile` dict fields (name, projects, `project_sector_mapping`, etc.), never the PDF text directly, so as long as `state.json`'s `profile` block is a thorough summary (it should already be, since that's how the existing profile was built), the PDFs add nothing the routine can use.

**There is no "update file" tool for Drive** — only create. So the read/write pattern the routine prompt must use is:
- **Read**: search `parentId = 'FOLDER_ID' and title = 'state.json'`, take the result with the newest `createdTime` (ignore older ones — they're prior runs' leftovers).
- **Write**: create a *new* file with the same title in the same folder. Don't try to delete the old one (no delete tool either) — the next run's "take the newest" logic handles it. This does mean the folder accumulates versions over time, which doubles as a crude audit trail; if that becomes noisy, that's a future cleanup problem, not a blocker now.

## Broadening discovery beyond job boards

This is the part a plain "search Indeed" routine misses, and it's usually why the user asked for this skill in the first place: most people worth reaching out to at an early-stage company haven't posted a job listing anywhere yet. Build the routine's search step around at least these categories, adapting the region-specific ones to wherever the user is job-hunting:

- **Job boards**: Indeed (use a dedicated connector if one showed up in the `mcp_connections` check — faster and more precise than scraping); ATS boards directly via `site:jobs.lever.co`, `site:boards.greenhouse.io`, `site:jobs.ashbyhq.com`, `site:apply.workable.com` searches.
- **Startup directories (global)**: Y Combinator's company directory (ycombinator.com/companies, filtered to recent batches + "hiring"), Product Hunt's trending/launched section.
- **Startup directories (regional)** — ask the user, or infer from their profile's `location` field, which local startup ecosystem to include. Examples: for Turkey, İTÜ Çekirdek's portfolio and Webrazzi's funding/startup news; other regions have their own equivalents (a local accelerator's portfolio page, a regional tech-news outlet's funding-roundup coverage). Don't hardcode a single country's sources into the routine prompt without asking — this list should match where the user is actually looking.

For each startup found this way, search for its careers page / ATS link the same way you would for a company found via a job board — the point isn't a different pipeline, just a wider net feeding the same `pipeline.py` filter.

## Building and creating the routine

1. Draft the full routine prompt from `references/routine_prompt_template.md`, filling in the placeholders (repo URL, Drive folder/file IDs, region-specific discovery sources). Keep every rule from the template — the zero-tolerance filter, the "never guess personal emails," the "never send, only draft," the "never touch `outreachos` public repo with writes" instruction — cloud agents get a fresh git checkout every run with zero memory of this conversation, so anything not in the prompt itself doesn't exist to them.
2. Before handing off, confirm `RemoteTrigger` is actually reachable in *this* context — load it with `ToolSearch select:RemoteTrigger`. It's a main-session capability; if you're running as a spawned subagent (rather than the primary interactive session), it may not be loadable no matter how you search for it. Don't spend more than one or two attempts confirming this — if it's not found, treat it as unavailable and move to step 4, don't keep retrying.
3. If available: hand off to the **`schedule`** skill to actually create the routine (it owns the `RemoteTrigger` mechanics, cron/timezone conversion, and the required first `AskUserQuestion` call) — pass it the drafted prompt as the routine's task. Confirm the schedule (ask the user what time/cadence they want; daily mornings in their local timezone is the sensible default for "check what's new before I start my day"). After creation, offer a "run now" test (same `RemoteTrigger` `action: "run"`) so the user can see real output before waiting for the next scheduled fire, and tell them where to check results: the Drive folder, their Gmail drafts, and https://claude.ai/code/routines/{id} for the run log.
4. If `RemoteTrigger` is genuinely unavailable: don't just report the blocker and stop — see "Fallback: GitHub Actions cron" below. Only skip the fallback if the user explicitly says they'd rather wait and do this from a session where routines work.

## Fallback: GitHub Actions cron (when `RemoteTrigger` isn't reachable)

Claude Code cloud routines are the default because they need no secrets management and no new account — but if `RemoteTrigger` isn't available in your current context, the daily-cron requirement can still be met with GitHub Actions, which runs on GitHub's own servers and is just as independent of the user's machine. This is a real fallback, not a consolation prize — treat it as a complete deliverable, not a note that "the real solution needs RemoteTrigger."

1. **Create a new private repo** (`gh repo create <name> --private`) to hold the automation — keep it separate from the public `outreachos` fork, same reasoning as the Drive-not-a-second-repo split: this repo *does* hold real secrets (search API key, Gmail OAuth credentials as repo secrets), so it must stay private, unlike the code repo.
2. **Clone the code repo read-only at workflow run time** rather than duplicating `pipeline.py` into the new repo — that way rule changes in the upstream `outreachos` repo propagate automatically instead of silently drifting out of sync.
3. **Write a small dependency-free script** (matching the upstream project's own stdlib-only philosophy) that: loads the user's profile/rules (from a repo secret or committed non-sensitive config), searches the sources from "Broadening discovery" above, runs each candidate through the cloned `pipeline.py`'s `decide()`, and — if Gmail credentials are present as secrets — creates a draft via `src/gmail.py`'s `create_draft` (still never `send`). Have it no-op cleanly (exit 0) if required secrets aren't set yet, so an accidentally-enabled workflow doesn't fail loudly.
4. **Cron in the workflow YAML must be UTC**, exactly like `RemoteTrigger` — the same local-time-to-UTC conversion applies (e.g. 09:00 Europe/Istanbul → `0 6 * * *`). Getting this wrong is an easy, easy-to-miss mistake if you're hand-converting instead of using the `schedule` skill's explicit conversion step — double check the arithmetic, don't eyeball it.
5. **Create the workflow disabled** (`gh workflow disable` right after pushing) until the user has added their real secrets and reviewed it — never leave a freshly-created, untested automation live and enabled.
6. Tell the user clearly this is a *different trust model* than the Drive+Gmail-connector path: their search-API and Gmail OAuth credentials would live as GitHub Actions secrets in their own repo rather than being handled invisibly through Claude connectors. Give them the exact steps to add those secrets and enable the workflow themselves — don't add real credentials on their behalf.

## Final report to the user

State plainly: what got created (folder link, routine link, schedule in their local time), what data now lives where (code=public repo, everything else=Drive), and reassure explicitly that none of it depends on their computer being on — the routine is a cloud session triggered server-side, same as any other cron job, just hosted by Anthropic instead of the user's machine.
