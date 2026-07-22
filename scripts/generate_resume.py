#!/usr/bin/env python3
"""Generate a living intelligence dossier SVG from GitHub profile data."""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image, ImageEnhance, ImageOps

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPTS / "config.yaml"
TEMPLATE_DIR = SCRIPTS / "templates"
OUTPUT_PATH = ROOT / "assets" / "javohir_casefile_live.svg"

API = "https://api.github.com"
GRAPHQL = f"{API}/graphql"
USER_AGENT = "javokhir-dossier-generator/2.0"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _headers(token: str | None = None, accept: str = "application/vnd.github+json") -> dict[str, str]:
    h = {
        "Accept": accept,
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def http_get(url: str, token: str | None = None, accept: str = "application/vnd.github+json") -> Any:
    req = urllib.request.Request(url, headers=_headers(token, accept))
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype or url.startswith(API):
                return json.loads(raw.decode("utf-8"))
            return raw
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed: {e.code} {body[:300]}") from e


def http_get_bytes(url: str, token: str | None = None) -> bytes:
    req = urllib.request.Request(url, headers=_headers(token, "image/*,*/*"))
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read()


def graphql(query: str, variables: dict[str, Any], token: str) -> dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        GRAPHQL,
        data=payload,
        headers={**_headers(token, "application/json"), "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data["data"]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def fmt_int(n: int | None) -> str:
    if n is None:
        return "—"
    return f"{n:,}"


def relative_time(iso: str | None) -> str:
    if not iso:
        return "UNKNOWN"
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        m = secs // 60
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if secs < 86400:
        h = secs // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    if secs < 86400 * 30:
        d = secs // 86400
        return f"{d} day{'s' if d != 1 else ''} ago"
    if secs < 86400 * 365:
        mo = secs // (86400 * 30)
        return f"{mo} month{'s' if mo != 1 else ''} ago"
    y = secs // (86400 * 365)
    return f"{y} year{'s' if y != 1 else ''} ago"


def xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def star_glyphs(count: int, max_stars: int = 10) -> str:
    """Map star count to a 10-slot filled/empty bar."""
    if max_stars <= 0:
        filled = 0
    else:
        # Log-ish scale: 0→0, 1→3, 5→6, 20→8, 100+→10
        if count <= 0:
            filled = 0
        elif count == 1:
            filled = 3
        elif count < 5:
            filled = 5
        elif count < 15:
            filled = 7
        elif count < 50:
            filled = 8
        elif count < 100:
            filled = 9
        else:
            filled = 10
    return "★" * filled + "☆" * (10 - filled)


def truncate(text: str | None, n: int) -> str:
    if not text:
        return "—"
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------------------
# GitHub fetch
# ---------------------------------------------------------------------------

def fetch_user(username: str, token: str | None) -> dict[str, Any]:
    return http_get(f"{API}/users/{username}", token)


def fetch_orgs(username: str, token: str | None) -> list[dict[str, Any]]:
    try:
        return http_get(f"{API}/users/{username}/orgs", token) or []
    except RuntimeError:
        return []


def fetch_repos(username: str, token: str | None) -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = http_get(
            f"{API}/users/{username}/repos?per_page=100&page={page}&type=owner&sort=updated",
            token,
        )
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        if page > 10:
            break
    return [r for r in repos if not r.get("fork")]


def fetch_events(username: str, token: str | None) -> list[dict[str, Any]]:
    try:
        return http_get(f"{API}/users/{username}/events/public?per_page=30", token) or []
    except RuntimeError:
        return []


def fetch_repo_languages(owner: str, repo: str, token: str | None) -> dict[str, int]:
    try:
        return http_get(f"{API}/repos/{owner}/{repo}/languages", token) or {}
    except RuntimeError:
        return {}


def fetch_graphql_stats(username: str, token: str) -> dict[str, Any]:
    """Commits, PRs, issues, contribution calendar for streaks."""
    now = datetime.now(timezone.utc)
    # Sum commits across recent years (GitHub returns one year window per query)
    years = list(range(now.year, now.year - 5, -1))
    total_commits = 0
    calendar_days: list[dict[str, Any]] = []

    year_query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          totalCommitContributions
          totalPullRequestContributions
          totalIssueContributions
          totalPullRequestReviewContributions
          contributionCalendar {
            weeks {
              contributionDays { date contributionCount }
            }
          }
        }
      }
    }
    """

    pr_total = 0
    issue_total = 0
    review_total = 0

    for year in years:
        frm = f"{year}-01-01T00:00:00Z"
        to = f"{year}-12-31T23:59:59Z"
        if year == now.year:
            to = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            data = graphql(
                year_query,
                {"login": username, "from": frm, "to": to},
                token,
            )
        except RuntimeError as e:
            print(f"  warn: GraphQL year {year}: {e}", file=sys.stderr)
            continue
        user = data.get("user") or {}
        cc = user.get("contributionsCollection") or {}
        total_commits += int(cc.get("totalCommitContributions") or 0)
        if year == now.year or year == years[0]:
            pr_total = max(pr_total, int(cc.get("totalPullRequestContributions") or 0))
            issue_total = max(issue_total, int(cc.get("totalIssueContributions") or 0))
            review_total = max(review_total, int(cc.get("totalPullRequestReviewContributions") or 0))
        # Keep calendar from most recent complete window
        if year == now.year:
            for week in (cc.get("contributionCalendar") or {}).get("weeks") or []:
                calendar_days.extend(week.get("contributionDays") or [])

    # Also try lifetime search counts for PRs/issues authored
    search_query = """
    query($prq: String!, $isq: String!) {
      prs: search(query: $prq, type: ISSUE, first: 0) { issueCount }
      issues: search(query: $isq, type: ISSUE, first: 0) { issueCount }
    }
    """
    try:
        sdata = graphql(
            search_query,
            {
                "prq": f"author:{username} type:pr",
                "isq": f"author:{username} type:issue",
            },
            token,
        )
        pr_total = max(pr_total, int((sdata.get("prs") or {}).get("issueCount") or 0))
        issue_total = max(issue_total, int((sdata.get("issues") or {}).get("issueCount") or 0))
    except RuntimeError as e:
        print(f"  warn: GraphQL search: {e}", file=sys.stderr)

    current_streak, longest_streak = compute_streaks(calendar_days)

    return {
        "commits": total_commits,
        "pull_requests": pr_total,
        "issues": issue_total,
        "reviews": review_total,
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "partial": False,
    }


def compute_streaks(days: list[dict[str, Any]]) -> tuple[int, int]:
    if not days:
        return 0, 0
    # Sort by date ascending
    sorted_days = sorted(days, key=lambda d: d.get("date", ""))
    longest = 0
    run = 0
    for d in sorted_days:
        if int(d.get("contributionCount") or 0) > 0:
            run += 1
            longest = max(longest, run)
        else:
            run = 0

    # Current streak: walk backward from today/yesterday
    by_date = {d["date"]: int(d.get("contributionCount") or 0) for d in sorted_days if "date" in d}
    current = 0
    cursor = datetime.now(timezone.utc).date()
    # Allow yesterday start if today is empty
    if by_date.get(cursor.isoformat(), 0) == 0:
        from datetime import timedelta

        cursor = cursor - timedelta(days=1)
    from datetime import timedelta

    while by_date.get(cursor.isoformat(), 0) > 0:
        current += 1
        cursor = cursor - timedelta(days=1)

    return current, longest


# ---------------------------------------------------------------------------
# Avatar
# ---------------------------------------------------------------------------

def build_mugshot(avatar_url: str, token: str | None) -> str:
    """Download avatar, convert to grayscale JPEG, return data URI."""
    raw = http_get_bytes(avatar_url, token)
    img = Image.open(BytesIO(raw)).convert("RGB")
    img = ImageOps.fit(img, (280, 360), method=Image.Resampling.LANCZOS)
    img = ImageOps.grayscale(img).convert("RGB")
    img = ImageEnhance.Contrast(img).enhance(1.15)
    img = ImageEnhance.Brightness(img).enhance(0.95)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=82)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

EVENT_MAP = {
    "PushEvent": "Pushed commits",
    "CreateEvent": "Created repository / branch",
    "DeleteEvent": "Deleted branch / tag",
    "PullRequestEvent": "Opened / updated pull request",
    "PullRequestReviewEvent": "Reviewed pull request",
    "IssuesEvent": "Opened / updated issue",
    "IssueCommentEvent": "Commented on issue",
    "WatchEvent": "Starred a repository",
    "ForkEvent": "Forked a repository",
    "ReleaseEvent": "Published release",
    "PublicEvent": "Open-sourced a repository",
    "MemberEvent": "Updated collaborator access",
    "CommitCommentEvent": "Commented on commit",
}


def map_event(event: dict[str, Any]) -> str:
    etype = event.get("type", "")
    payload = event.get("payload") or {}
    repo = (event.get("repo") or {}).get("name", "")
    short_repo = repo.split("/")[-1] if repo else ""

    if etype == "PushEvent":
        commits = payload.get("commits") or []
        n = len(commits) or payload.get("size") or 1
        msg = truncate(commits[0].get("message") if commits else None, 40)
        base = f"Pushed {n} commit{'s' if n != 1 else ''}"
        return f"{base} → {short_repo}" if short_repo else base if not msg or msg == "—" else f"{base}: {msg}"
    if etype == "PullRequestEvent":
        action = payload.get("action", "updated")
        return f"{action.capitalize()} pull request → {short_repo}".strip()
    if etype == "IssuesEvent":
        action = payload.get("action", "updated")
        return f"{action.capitalize()} issue → {short_repo}".strip()
    if etype == "CreateEvent":
        ref_type = payload.get("ref_type", "repository")
        return f"Created {ref_type} → {short_repo}".strip()
    if etype == "ReleaseEvent":
        tag = (payload.get("release") or {}).get("tag_name", "")
        return f"Published release {tag} → {short_repo}".strip()
    label = EVENT_MAP.get(etype, etype.replace("Event", ""))
    return f"{label} → {short_repo}".strip(" →")


def analyze_languages(repos: list[dict[str, Any]], username: str, token: str | None, limit: int) -> list[dict[str, Any]]:
    totals: Counter[str] = Counter()
    for repo in repos[:40]:
        langs = fetch_repo_languages(username, repo["name"], token)
        if langs:
            totals.update(langs)
        elif repo.get("language"):
            totals[repo["language"]] += 1
    if not totals:
        return []
    grand = sum(totals.values()) or 1
    top = totals.most_common(limit)
    max_bytes = top[0][1] if top else 1
    bars = []
    for name, nbytes in top:
        pct = round(100 * nbytes / grand)
        width = int(round(170 * (nbytes / max_bytes)))
        bars.append({"name": name, "pct": pct, "width": max(width, 4)})
    return bars


def detect_frameworks(repos: list[dict[str, Any]], username: str, config: dict[str, Any], token: str | None) -> dict[str, list[str]]:
    curated = config.get("frameworks") or {}
    result = {k: list(v) for k, v in curated.items()}
    signals: dict[str, list[str]] = config.get("framework_signals") or {}
    # Light detection: check default branch tree is expensive; use language + topics + description
    lang_hints = {
        "Python": ["Django", "Django REST Framework", "FastAPI"],
        "TypeScript": ["React", "Next.js", "Axios"],
        "JavaScript": ["React", "Express.js", "Axios"],
        "Go": ["Go"],
        "Dockerfile": ["Docker"],
    }
    detected: set[str] = set()
    for repo in repos[:25]:
        lang = repo.get("language")
        if lang in lang_hints:
            detected.update(lang_hints[lang])
        for topic in repo.get("topics") or []:
            t = topic.lower()
            for cat_items in curated.values():
                for item in cat_items:
                    if item.lower().replace(" ", "") in t.replace("-", "") or t in item.lower():
                        detected.add(item)
        # Optional: peek at repo contents root
        try:
            contents = http_get(f"{API}/repos/{username}/{repo['name']}/contents/", token)
            if isinstance(contents, list):
                names = {c.get("name", "") for c in contents}
                for signal, labels in signals.items():
                    base = signal.strip("/").split("/")[0]
                    if any(n == base or n.startswith(base) for n in names):
                        detected.update(labels)
        except RuntimeError:
            pass

    # Merge detected into matching categories by membership in curated lists
    curated_lookup = {item: cat for cat, items in curated.items() for item in items}
    for item in detected:
        cat = curated_lookup.get(item)
        if cat and item not in result[cat]:
            result[cat].append(item)
    return result


def build_context(config: dict[str, Any], token: str | None) -> dict[str, Any]:
    username = config["github_username"]
    print(f"→ Fetching profile @{username} …")
    user = fetch_user(username, token)
    orgs = fetch_orgs(username, token)
    print("→ Fetching repositories …")
    repos = fetch_repos(username, token)
    print("→ Fetching public events …")
    events = fetch_events(username, token)

    gql: dict[str, Any] = {
        "commits": None,
        "pull_requests": None,
        "issues": None,
        "current_streak": None,
        "longest_streak": None,
        "partial": True,
    }
    if token:
        print("→ Fetching GraphQL contribution stats …")
        try:
            gql = fetch_graphql_stats(username, token)
        except RuntimeError as e:
            print(f"  warn: GraphQL unavailable: {e}", file=sys.stderr)
    else:
        print("→ No GITHUB_TOKEN — using REST-only (PARTIAL CLEARANCE)")

    print("→ Building mugshot …")
    avatar_url = user.get("avatar_url") or f"https://github.com/{username}.png"
    try:
        mugshot = build_mugshot(f"{avatar_url}&s=400" if "?" in avatar_url else f"{avatar_url}?s=400", token)
    except Exception as e:
        print(f"  warn: avatar failed ({e}), using placeholder", file=sys.stderr)
        mugshot = ""

    print("→ Analyzing languages …")
    languages = analyze_languages(repos, username, token, config.get("language_bar_count", 6))

    print("→ Detecting frameworks …")
    frameworks = detect_frameworks(repos, username, config, token)

    stars = sum(int(r.get("stargazers_count") or 0) for r in repos)
    forks = sum(int(r.get("forks_count") or 0) for r in repos)

    # High value assets
    ranked = sorted(
        repos,
        key=lambda r: (int(r.get("stargazers_count") or 0), r.get("pushed_at") or ""),
        reverse=True,
    )
    n_assets = int(config.get("high_value_assets_count", 4))
    assets = []
    max_s = max((int(r.get("stargazers_count") or 0) for r in ranked[:n_assets]), default=0)
    for r in ranked[:n_assets]:
        sc = int(r.get("stargazers_count") or 0)
        assets.append(
            {
                "name": r.get("name") or "—",
                "description": truncate(r.get("description"), 48),
                "stars": sc,
                "star_bar": star_glyphs(sc if max_s else sc),
                "language": r.get("language") or "—",
                "updated": relative_time(r.get("pushed_at")),
            }
        )

    # Active operations from latest push / most recently pushed repo
    active_repo = ranked[0] if ranked else None
    last_commit_msg = "—"
    last_branch = "—"
    for ev in events:
        if ev.get("type") == "PushEvent":
            payload = ev.get("payload") or {}
            commits = payload.get("commits") or []
            if commits:
                last_commit_msg = truncate(commits[-1].get("message"), 52)
            ref = payload.get("ref") or ""
            if ref.startswith("refs/heads/"):
                last_branch = ref.replace("refs/heads/", "")
            repo_name = ((ev.get("repo") or {}).get("name") or "").split("/")[-1]
            if repo_name:
                active_repo = next((r for r in repos if r["name"] == repo_name), active_repo)
            break

    last_seen_iso = events[0].get("created_at") if events else user.get("updated_at")
    created = user.get("created_at") or ""
    github_since = created[:4] if created else "—"
    years_active = "—"
    if created:
        cy = int(created[:4])
        years_active = str(max(1, datetime.now(timezone.utc).year - cy))

    org_names = ", ".join(o.get("login", "") for o in orgs) or "NONE"
    subject = config.get("subject") or {}

    primary_language = languages[0]["name"] if languages else "—"
    most_active = active_repo.get("name") if active_repo else "—"

    intel_n = int(config.get("recent_intelligence_count", 8))
    intelligence = []
    seen = set()
    for ev in events:
        line = map_event(ev)
        if line in seen:
            continue
        seen.add(line)
        intelligence.append(line)
        if len(intelligence) >= intel_n:
            break
    while len(intelligence) < min(3, intel_n):
        intelligence.append("No recent public activity recorded")
        break

    now = datetime.now(timezone.utc)
    clearance_note = "PARTIAL CLEARANCE" if gql.get("partial") else "VERIFIED"

    # Language bar geometry helpers for template
    for lang in languages:
        lang["name_esc"] = xml_escape(lang["name"])

    layout = compute_layout(len(languages), len(assets))
    for i, a in enumerate(assets):
        col = i % 2
        row = i // 2
        a["x"] = 30 if col == 0 else 435
        a["y"] = layout["assets_box"] + row * 100

    ctx = {
        "L": layout,
        "canvas_height": layout["canvas_height"],
        "case_file": config.get("case_file", "0609"),
        "document_id": config.get("document_id", "CF-0609-A"),
        "revision": config.get("revision", "2.0"),
        "display_name": xml_escape(user.get("name") or subject.get("display_name") or username),
        "display_name_upper": xml_escape((user.get("name") or subject.get("display_name") or username).upper()),
        "codename": xml_escape(username),
        "classification": xml_escape(subject.get("classification", "")),
        "status": xml_escape(subject.get("status", "ACTIVE")),
        "threat_level": xml_escape(subject.get("threat_level", "HIGH")),
        "clearance": xml_escape(subject.get("clearance", "FULLSTACK ENGINEER")),
        "location": xml_escape(user.get("location") or subject.get("location_fallback") or "—"),
        "phone": xml_escape(subject.get("phone", "—")),
        "email": xml_escape(truncate(subject.get("email", "—"), 36)),
        "linkedin": xml_escape(truncate(subject.get("linkedin", "—"), 34)),
        "website": xml_escape(truncate(user.get("blog") or "—", 28)) if user.get("blog") else "—",
        "bio": xml_escape(truncate(user.get("bio"), 80)),
        "github_since": github_since,
        "followers": user.get("followers", 0),
        "following": user.get("following", 0),
        "public_repos": user.get("public_repos", 0),
        "orgs": xml_escape(org_names),
        "last_seen": relative_time(last_seen_iso),
        "mugshot": mugshot,
        "has_mugshot": bool(mugshot),
        "summary": [xml_escape(line) for line in (config.get("summary") or [])],
        # Mission record
        "ops_completed": fmt_int(user.get("public_repos")),
        "deployments": fmt_int(gql.get("commits")),
        "associates": fmt_int(user.get("followers")),
        "intel_sources": fmt_int(user.get("following")),
        "pull_requests": fmt_int(gql.get("pull_requests")),
        "cases_closed": fmt_int(gql.get("issues")),
        "stars_earned": fmt_int(stars),
        "forked_ops": fmt_int(forks),
        "latest_deployment": relative_time(last_seen_iso),
        "mission_status": xml_escape(subject.get("status", "ACTIVE")),
        # Arsenal
        "languages": languages,
        "frameworks": {k: [xml_escape(x) for x in v] for k, v in frameworks.items()},
        # Assets
        "assets": [
            {
                **a,
                "name": xml_escape(a["name"]),
                "description": xml_escape(a["description"]),
                "language": xml_escape(a["language"]),
            }
            for a in assets
        ],
        # Active ops
        "active_repo": xml_escape(active_repo.get("name") if active_repo else "—"),
        "active_branch": xml_escape(last_branch),
        "active_commit": xml_escape(last_commit_msg),
        "active_time": relative_time(
            (active_repo or {}).get("pushed_at") if active_repo else last_seen_iso
        ),
        "active_mission": xml_escape(
            truncate((active_repo or {}).get("description"), 40) if active_repo else "—"
        ),
        "intelligence": [xml_escape(x) for x in intelligence],
        # Threat / field notes
        "threat_assessment": config.get("threat_assessment") or [],
        "field_notes": config.get("field_notes") or {},
        "known_for": [xml_escape(x) for x in ((config.get("field_notes") or {}).get("known_for") or [])],
        "field_status": xml_escape((config.get("field_notes") or {}).get("current_status", "MISSION ACTIVE")),
        # Operational metrics
        "primary_language": xml_escape(primary_language),
        "most_active_repo": xml_escape(str(most_active)),
        "years_active": years_active,
        "current_streak": fmt_int(gql.get("current_streak")),
        "longest_streak": fmt_int(gql.get("longest_streak")),
        "partial": bool(gql.get("partial")),
        "clearance_note": clearance_note,
        "generated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
        "generated_date": now.strftime("%Y-%m-%d"),
    }
    return ctx


def compute_layout(n_languages: int, n_assets: int) -> dict[str, int]:
    """Precompute vertical positions so the SVG canvas fits all sections."""
    lang_rows = max(n_languages, 1)
    arsenal_h = 28 + lang_rows * 28
    arsenal_rule = 614
    arsenal_title = 640
    arsenal_rule2 = 650
    arsenal_box = 666

    fw_rule = arsenal_box + arsenal_h + 20
    fw_title = fw_rule + 26
    fw_rule2 = fw_rule + 36
    fw_row1 = fw_rule + 52
    fw_row2 = fw_row1 + 176

    assets_rule = fw_row2 + 140
    assets_title = assets_rule + 26
    assets_rule2 = assets_rule + 36
    assets_box = assets_rule + 52
    asset_rows = max((n_assets + 1) // 2, 1) if n_assets else 1
    assets_block_h = asset_rows * 100 if n_assets else 56

    ops_rule = assets_box + assets_block_h + 16
    ops_title = ops_rule + 26
    ops_rule2 = ops_rule + 36
    ops_box = ops_rule + 52

    threat_rule = ops_box + 170
    threat_title = threat_rule + 26
    threat_rule2 = threat_rule + 36
    threat_box = threat_rule + 52

    metrics_rule = threat_box + 220
    metrics_title = metrics_rule + 26
    metrics_rule2 = metrics_rule + 36
    metrics_box = metrics_rule + 52

    footer_rule = metrics_box + 108
    footer_meta = footer_rule + 36
    canvas_height = footer_meta + 80

    return {
        "arsenal_rule": arsenal_rule,
        "arsenal_title": arsenal_title,
        "arsenal_rule2": arsenal_rule2,
        "arsenal_box": arsenal_box,
        "arsenal_h": arsenal_h,
        "fw_rule": fw_rule,
        "fw_title": fw_title,
        "fw_rule2": fw_rule2,
        "fw_row1": fw_row1,
        "fw_row2": fw_row2,
        "assets_rule": assets_rule,
        "assets_title": assets_title,
        "assets_rule2": assets_rule2,
        "assets_box": assets_box,
        "ops_rule": ops_rule,
        "ops_title": ops_title,
        "ops_rule2": ops_rule2,
        "ops_box": ops_box,
        "threat_rule": threat_rule,
        "threat_title": threat_title,
        "threat_rule2": threat_rule2,
        "threat_box": threat_box,
        "metrics_rule": metrics_rule,
        "metrics_title": metrics_title,
        "metrics_rule2": metrics_rule2,
        "metrics_box": metrics_box,
        "footer_rule": footer_rule,
        "footer_meta": footer_meta,
        "canvas_height": canvas_height,
    }


def render(ctx: dict[str, Any]) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(enabled_extensions=()),
    )
    for item in ctx["threat_assessment"]:
        score = int(item.get("score") or 0)
        item["width"] = max(4, int(round(170 * score / 100)))
        item["label_esc"] = xml_escape(item.get("label", ""))
    template = env.get_template("dossier.svg.j2")
    return template.render(**ctx)


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    ctx = build_context(config, token)
    svg = render(ctx)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(svg, encoding="utf-8")

    print()
    print(f"✓ Wrote {OUTPUT_PATH.relative_to(ROOT)}")
    print(f"  Subject     {ctx['display_name']} (@{ctx['codename']})")
    print(f"  Operations  {ctx['ops_completed']} repos")
    print(f"  Associates  {ctx['associates']} followers")
    print(f"  Stars       {ctx['stars_earned']}")
    print(f"  Deployments {ctx['deployments']}")
    print(f"  Clearance   {ctx['clearance_note']}")
    print(f"  Generated   {ctx['generated_at']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"✗ {exc}", file=sys.stderr)
        raise
