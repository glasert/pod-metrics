"""
fetch_prs.py
────────────
Refreshes the data files served by the dashboard. Run by GitHub Actions.

Reads the curated repo list from tracked-repos.txt (one repo name per line,
'#' comments allowed) and fetches merged PRs for each.

Two outputs:

  repos.json — list of tracked repos for the dashboard dropdown
  {
    "generated_at": "...",
    "org": "adobecom",
    "repos": ["mas", "mas-pinata", "milo", "milo-pinata"]
  }

  data.json — PR data for those repos
  {
    "generated_at": "...",
    "org": "adobecom",
    "tracked_repos": ["adobecom/mas", "adobecom/mas-pinata", ...],
    "max_age_days": 365,
    "prs": [
      {
        number, title, repo, author,
        first_commit_at,  # ISO8601 — earliest commit on the PR branch (or null)
        created_at,       # ISO8601 — PR opened
        merged_at,        # ISO8601 — PR merged
        dev_days,         # first_commit_at → created_at (null if no commits)
        lead_days,        # created_at → merged_at (review/merge time)
        url
      }, ...
    ]
  }

Environment variables:
  GITHUB_TOKEN     — provided by GitHub Actions
  ORG              — GitHub org these repos live under (default: "adobecom")
  CONFIG_FILE      — path to repo list (default: "tracked-repos.txt")
  MAX_AGE_DAYS     — PRs older than this are excluded (default 365)
  MAX_PRS          — per-repo cap on merged PRs to fetch (default 500)
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
CONFIG_FILE = os.environ.get("CONFIG_FILE", "tracked-repos.txt")
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "365"))
MAX_PRS = int(os.environ.get("MAX_PRS", "500"))

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


def read_tracked_repos(path):
    """Parse the config file. Returns sorted list of bare repo names."""
    if not os.path.exists(path):
        print(f"ERROR: config file '{path}' not found")
        sys.exit(1)
    names = []
    with open(path) as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            # Tolerate accidental "owner/name" entries by stripping the prefix
            if "/" in line:
                line = line.split("/", 1)[1]
            names.append(line)
    seen = set()
    deduped = [n for n in names if not (n in seen or seen.add(n))]
    return sorted(deduped)


def first_commit_at(full_name, pr_number):
    """Return committer-date of the earliest commit on the PR's branch, or None.

    GitHub returns commits in chronological order (oldest first) via the
    /pulls/{N}/commits endpoint. Page 1 with per_page=100 gives us the start
    of the branch in one call, which covers the vast majority of PRs. If a
    PR has 100+ commits, we still get the earliest from page 1 — no extra
    pagination needed.
    """
    url = f"https://api.github.com/repos/{full_name}/pulls/{pr_number}/commits"
    params = {"per_page": 100, "page": 1}
    batch = get_json(url, params=params)
    if not batch:
        return None
    first = batch[0]
    committer = first.get("commit", {}).get("committer", {})
    return parse_dt(committer.get("date"))


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
            print(f"    WARN: {full_name} returned 404 — check the repo name in {CONFIG_FILE}")
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

            # Fetch the earliest commit on the branch for dev-time metric.
            # One extra API call per PR. Failures (deleted branch, etc.) are
            # tolerated — we just record null dev_days for that PR.
            try:
                first_at = first_commit_at(full_name, pr["number"])
            except requests.HTTPError as e:
                print(f"      WARN: PR #{pr['number']} commits fetch failed ({e})")
                first_at = None

            if first_at is not None:
                dev_seconds = (created_at - first_at).total_seconds()
                # Guard against negative values (rare clock-skew edge case)
                dev_days = round(max(dev_seconds, 0) / 86400, 3)
                first_commit_iso = first_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                dev_days = None
                first_commit_iso = None

            out.append({
                "number": pr["number"],
                "title": pr["title"],
                "repo": full_name,
                "author": (pr.get("user") or {}).get("login", "unknown"),
                "first_commit_at": first_commit_iso,
                "created_at": pr["created_at"],
                "merged_at": pr["merged_at"],
                "dev_days": dev_days,
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
    print(f"Config: {CONFIG_FILE}")
    print(f"Window: last {MAX_AGE_DAYS} days, cap {MAX_PRS}/repo\n")

    tracked = read_tracked_repos(CONFIG_FILE)
    if not tracked:
        print(f"ERROR: no repos listed in {CONFIG_FILE}")
        sys.exit(1)
    print(f"Tracked repos ({len(tracked)}): {', '.join(tracked)}\n")

    # Write the repo list immediately — dropdown depends on it
    timestamp = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    repos_payload = {
        "generated_at": timestamp,
        "org": ORG,
        "repos": tracked,
    }
    with open("repos.json", "w") as f:
        json.dump(repos_payload, f, separators=(",", ":"))
    print(f"Wrote repos.json — {len(tracked)} repos\n")

    print("Fetching PR data…")
    all_prs = []
    for name in tracked:
        all_prs.extend(fetch_merged_prs(f"{ORG}/{name}"))

    all_prs.sort(key=lambda p: p["merged_at"], reverse=True)

    data_payload = {
        "generated_at": timestamp,
        "org": ORG,
        "tracked_repos": [f"{ORG}/{n}" for n in tracked],
        "max_age_days": MAX_AGE_DAYS,
        "max_prs_per_repo": MAX_PRS,
        "prs": all_prs,
    }
    with open("data.json", "w") as f:
        json.dump(data_payload, f, separators=(",", ":"))

    print(f"\nWrote data.json — {len(all_prs)} PRs across {len(tracked)} repos")


if __name__ == "__main__":
    main()
