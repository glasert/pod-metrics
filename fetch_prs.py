"""
fetch_prs.py
────────────
Fetches merged pull requests from the configured repos and writes data.json
to the repo root. Run by .github/workflows/refresh.yml.

Output schema (data.json):
{
  "generated_at": "2026-05-12T06:00:00Z",
  "repos": ["adobecom/mas", "adobecom/mas-pinata", "adobecom/milo"],
  "prs": [
    {
      "number": 123,
      "title": "...",
      "repo": "adobecom/mas",
      "author": "octocat",
      "created_at": "2026-04-01T10:00:00Z",
      "merged_at": "2026-04-03T14:30:00Z",
      "lead_days": 2.19,
      "url": "https://github.com/..."
    },
    ...
  ]
}

Environment variables:
  GITHUB_TOKEN — provided automatically by GitHub Actions
  REPOS        — comma-separated "owner/repo" list (set in workflow env)
  MAX_PRS      — per-repo cap on merged PRs to fetch (default 500)
  MAX_AGE_DAYS — only include PRs merged within this many days (default 365)
"""

import os
import sys
import json
from datetime import datetime, timezone, timedelta

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests")
    sys.exit(1)

TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPOS = [r.strip() for r in os.environ.get(
    "REPOS",
    "adobecom/mas,adobecom/mas-pinata,adobecom/milo"
).split(",") if r.strip()]
MAX_PRS = int(os.environ.get("MAX_PRS", "500"))
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "365"))

if not TOKEN:
    print("ERROR: GITHUB_TOKEN not set.")
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(days=MAX_AGE_DAYS)


def parse_dt(s):
    if not s:
        return None
    # GitHub returns Zulu time: "2026-04-01T10:00:00Z"
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fetch_merged_prs(repo):
    """Fetch merged PRs for one repo, newest first, up to MAX_PRS or CUTOFF."""
    print(f"  Fetching {repo} …", flush=True)
    out = []
    page = 1
    per_page = 100
    while len(out) < MAX_PRS:
        url = f"https://api.github.com/repos/{repo}/pulls"
        params = {
            "state": "closed",
            "sort": "updated",
            "direction": "desc",
            "per_page": per_page,
            "page": page,
        }
        resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
        if resp.status_code == 404:
            print(f"    WARN: {repo} returned 404 — skipping")
            return []
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break

        stop = False
        for pr in batch:
            merged_at = parse_dt(pr.get("merged_at"))
            if not merged_at:
                continue  # closed without merge — skip
            if merged_at < CUTOFF:
                # Sorted by updated desc, so we *might* still see older merges
                # that were recently updated; keep going through this page,
                # but signal stop after the loop.
                stop = True
                continue
            created_at = parse_dt(pr["created_at"])
            lead_seconds = (merged_at - created_at).total_seconds()
            lead_days = round(lead_seconds / 86400, 3)
            out.append({
                "number": pr["number"],
                "title": pr["title"],
                "repo": repo,
                "author": (pr.get("user") or {}).get("login", "unknown"),
                "created_at": pr["created_at"],
                "merged_at": pr["merged_at"],
                "lead_days": lead_days,
                "url": pr["html_url"],
            })
            if len(out) >= MAX_PRS:
                stop = True
                break

        if stop or len(batch) < per_page:
            break
        page += 1

    print(f"    {len(out)} merged PRs collected")
    return out


def main():
    print(f"Refreshing PR data at {NOW.isoformat()}")
    print(f"Repos: {REPOS}")
    print(f"Cutoff: merged within {MAX_AGE_DAYS} days, max {MAX_PRS}/repo")

    all_prs = []
    for repo in REPOS:
        all_prs.extend(fetch_merged_prs(repo))

    # Sort newest merged first for stable output
    all_prs.sort(key=lambda p: p["merged_at"], reverse=True)

    payload = {
        "generated_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repos": REPOS,
        "max_age_days": MAX_AGE_DAYS,
        "max_prs_per_repo": MAX_PRS,
        "prs": all_prs,
    }

    with open("data.json", "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    print(f"\nWrote data.json — {len(all_prs)} PRs total")


if __name__ == "__main__":
    main()
