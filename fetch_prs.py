"""
fetch_prs.py
────────────
Refreshes the data files served by the dashboard. Run by GitHub Actions.

Two outputs:

  repos.json — full list of the org's repos, for the dropdown
  {
    "generated_at": "2026-05-12T06:00:00Z",
    "org": "adobecom",
    "repos": ["mas", "mas-pinata", "milo", ...]   # name only, sorted
  }

  data.json — PR data for the auto-curated tracked set
  {
    "generated_at": "2026-05-12T06:00:00Z",
    "org": "adobecom",
    "tracked_repos": ["adobecom/mas", "adobecom/mas-pinata", ...],
    "tracked_criterion": "Merged PR within last 30 days",
    "max_age_days": 365,
    "prs": [ { number, title, repo, author, created_at, merged_at, lead_days, url }, ... ]
  }

Environment variables:
  GITHUB_TOKEN        — provided automatically by GitHub Actions
  ORG                 — GitHub org to scan (default: "adobecom")
  TRACKED_WINDOW_DAYS — a repo is tracked if it had a merged PR within this
                        window (default 30)
  MAX_AGE_DAYS        — PRs older than this are excluded (default 365)
  MAX_PRS             — per-repo cap on merged PRs to fetch (default 500)
  INCLUDE_FORKS       — "true" to include forks; default false
  INCLUDE_ARCHIVED    — "true" to include archived repos; default false
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
ORG = os.environ.get("ORG", "adobecom").strip()
TRACKED_WINDOW_DAYS = int(os.environ.get("TRACKED_WINDOW_DAYS", "30"))
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "365"))
MAX_PRS = int(os.environ.get("MAX_PRS", "500"))
INCLUDE_FORKS = os.environ.get("INCLUDE_FORKS", "false").lower() == "true"
INCLUDE_ARCHIVED = os.environ.get("INCLUDE_ARCHIVED", "false").lower() == "true"

if not TOKEN:
    print("ERROR: GITHUB_TOKEN not set.")
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

NOW = datetime.now(timezone.utc)
PR_CUTOFF = NOW - timedelta(days=MAX_AGE_DAYS)
TRACKED_CUTOFF = NOW - timedelta(days=TRACKED_WINDOW_DAYS)


def parse_dt(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def get_json(url, params=None):
    resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def list_org_repos(org):
    """All public repos in the org. Returns list of repo dicts."""
    print(f"Listing repos for {org}…", flush=True)
    out = []
    page = 1
    while True:
        params = {"per_page": 100, "page": page, "type": "public", "sort": "full_name"}
        batch = get_json(f"https://api.github.com/orgs/{org}/repos", params=params)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    print(f"  {len(out)} repos returned")
    return out


def filter_repos(repos):
    """Apply fork/archived filters."""
    kept = []
    for r in repos:
        if not INCLUDE_FORKS and r.get("fork"):
            continue
        if not INCLUDE_ARCHIVED and r.get("archived"):
            continue
        kept.append(r)
    return kept


def most_recent_merge(full_name):
    """Cheap probe: datetime of the most recently merged PR, or None.

    Walks closed PRs sorted by updated desc; first one with a merged_at wins.
    Scans only the first page (100 PRs) — sufficient for any repo with
    recent activity. Repos with no merge in their last 100 closed PRs are
    inactive enough to drop from tracking.
    """
    url = f"https://api.github.com/repos/{full_name}/pulls"
    params = {"state": "closed", "sort": "updated", "direction": "desc", "per_page": 100}
    batch = get_json(url, params=params)
    if not batch:
        return None
    for pr in batch:
        merged_at = parse_dt(pr.get("merged_at"))
        if merged_at:
            return merged_at
    return None


def pick_tracked_repos(repos):
    """Return repo dicts that had a merged PR within the window."""
    print(f"Checking which repos had a merge in the last {TRACKED_WINDOW_DAYS} days…", flush=True)
    tracked = []
    for r in repos:
        full = r["full_name"]
        try:
            last = most_recent_merge(full)
        except requests.HTTPError as e:
            print(f"  WARN: {full} probe failed ({e}) — skipping")
            continue
        if last and last >= TRACKED_CUTOFF:
            print(f"  ✓ {full}  (last merge {last.date()})")
            tracked.append(r)
    print(f"\n  {len(tracked)} repos qualify as tracked")
    return tracked


def fetch_merged_prs(full_name):
    """Fetch merged PRs for one repo, newest first, up to MAX_PRS or PR_CUTOFF."""
    print(f"  Fetching {full_name} …", flush=True)
    out = []
    page = 1
    while len(out) < MAX_PRS:
        url = f"https://api.github.com/repos/{full_name}/pulls"
        params = {
            "state": "closed",
            "sort": "updated",
            "direction": "desc",
            "per_page": 100,
            "page": page,
        }
        batch = get_json(url, params=params)
        if batch is None:
            return []
        if not batch:
            break

        stop = False
        for pr in batch:
            merged_at = parse_dt(pr.get("merged_at"))
            if not merged_at:
                continue
            if merged_at < PR_CUTOFF:
                stop = True
                continue
            created_at = parse_dt(pr["created_at"])
            lead_days = round((merged_at - created_at).total_seconds() / 86400, 3)
            out.append({
                "number": pr["number"],
                "title": pr["title"],
                "repo": full_name,
                "author": (pr.get("user") or {}).get("login", "unknown"),
                "created_at": pr["created_at"],
                "merged_at": pr["merged_at"],
                "lead_days": lead_days,
                "url": pr["html_url"],
            })
            if len(out) >= MAX_PRS:
                stop = True
                break

        if stop or len(batch) < 100:
            break
        page += 1

    print(f"    {len(out)} merged PRs collected")
    return out


def main():
    print(f"Refreshing data at {NOW.isoformat()}")
    print(f"Org: {ORG}")
    print(f"Tracked criterion: merge within last {TRACKED_WINDOW_DAYS} days")
    print(f"PR window: last {MAX_AGE_DAYS} days, cap {MAX_PRS}/repo\n")

    all_repos = list_org_repos(ORG)
    all_repos = filter_repos(all_repos)

    # Write the full repo list right away (dropdown only needs names)
    repos_payload = {
        "generated_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "org": ORG,
        "repos": sorted(r["name"] for r in all_repos),
    }
    with open("repos.json", "w") as f:
        json.dump(repos_payload, f, separators=(",", ":"))
    print(f"\nWrote repos.json — {len(repos_payload['repos'])} repos\n")

    tracked = pick_tracked_repos(all_repos)

    print("\nFetching PR data for tracked repos…")
    all_prs = []
    for r in tracked:
        all_prs.extend(fetch_merged_prs(r["full_name"]))

    all_prs.sort(key=lambda p: p["merged_at"], reverse=True)

    data_payload = {
        "generated_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "org": ORG,
        "tracked_repos": sorted(r["full_name"] for r in tracked),
        "tracked_criterion": f"Merged PR within last {TRACKED_WINDOW_DAYS} days",
        "max_age_days": MAX_AGE_DAYS,
        "max_prs_per_repo": MAX_PRS,
        "prs": all_prs,
    }
    with open("data.json", "w") as f:
        json.dump(data_payload, f, separators=(",", ":"))

    print(f"\nWrote data.json — {len(all_prs)} PRs across {len(tracked)} tracked repos")


if __name__ == "__main__":
    main()
