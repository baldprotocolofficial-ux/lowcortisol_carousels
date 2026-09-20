#!/usr/bin/env python3
"""Post approved, due carousels to Instagram. Built to run in a fresh cloud box.

This file is deliberately standalone. It imports nothing from the rest of the
skill, touches no local files, and keeps no state of its own, because the
container it runs in is empty when it starts and gone when it finishes.

Notion is the queue. A carousel is posted when, and only when, its page in the
Instagram Content Calendar says Status is Approved and the Scheduled date has
passed. Straight after posting the page is set to Posted, and that is what
stops the next run posting it again.

The slide images are already public. publish.py --cloud pushes them to the
GitHub image repo at scheduling time, so the file names are predictable:
    https://raw.githubusercontent.com/<owner>/<repo>/<branch>/<slug>/01.jpg

Environment it needs:
    IG_USER_ID, IG_ACCESS_TOKEN, GITHUB_REPO, NOTION_API_KEY, NOTION_DATABASE_ID
    GITHUB_TOKEN is optional and only lifts the GitHub rate limit.

Usage:
    python cloud_poster.py
    python cloud_poster.py --dry-run
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v23.0")
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"

APPROVED = "Approved"
POSTED = "Posted"
POSTING = "Posting"   # optional lock, used only if the option exists in Notion
FAILED = "Failed"

MAX_CHILDREN = 10
DAILY_PUBLISH_CAP = 20   # Meta allows 25

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def load_local_env():
    """Only for testing this file on the machine that renders.

    In the cloud there is no .env and this does nothing, which is the point:
    the runner takes its keys from the environment and nowhere else.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    here = Path.cwd().resolve()
    for folder in [Path(__file__).resolve().parent, *Path(__file__).resolve().parents,
                   here, *here.parents]:
        candidate = folder / ".env"
        if candidate.is_file():
            load_dotenv(candidate, override=False)


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def die(msg):
    print("\nSTOP: " + msg, file=sys.stderr, flush=True)
    sys.exit(1)


def need(name):
    value = (os.environ.get(name) or "").strip()
    if not value:
        die(
            f"{name} is not set. This runner reads every key from the environment, "
            f"never from a file."
        )
    return value


def normalize_repo(value):
    """Accept owner/repo, a browser url, or a git clone url. Return owner/repo.

    The cloud environment screen invites a full clone url, and the GitHub API
    wants neither that nor a .git suffix, so take whatever is given.
    """
    repo = (value or "").strip()
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:", "github.com/"):
        if repo.startswith(prefix):
            repo = repo[len(prefix):]
            break
    if repo.endswith(".git"):
        repo = repo[:-4]
    repo = repo.strip("/")
    parts = repo.split("/")
    if len(parts) != 2 or not all(parts):
        die(
            f"GITHUB_REPO is {value!r}. It must name one repository as owner/repo, "
            f"for example baldprotocolofficial-ux/larry-carousels."
        )
    return "/".join(parts)


# ------------------------------------------------------------------ notion --


def notion_headers():
    return {
        "Authorization": f"Bearer {need('NOTION_API_KEY')}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_request(method, url, **kwargs):
    resp = requests.request(method, url, headers=notion_headers(), timeout=45, **kwargs)
    if resp.status_code >= 300:
        detail = resp.text[:400]
        if resp.status_code == 404:
            detail += (
                "  (a 404 here almost always means the integration is not "
                "connected to the database. Open the database in Notion, "
                "... menu, Connections, add the integration.)"
            )
        raise RuntimeError(f"Notion {method} {resp.status_code}: {detail}")
    return resp.json()


def plain_text(prop):
    """Flatten a Notion title or rich_text property into a string."""
    if not prop:
        return ""
    parts = prop.get("title") or prop.get("rich_text") or []
    return "".join(p.get("plain_text", "") for p in parts)


def status_options():
    body = notion_request(
        "GET", f"{NOTION_API}/databases/{need('NOTION_DATABASE_ID')}"
    )
    select = (body.get("properties", {}).get("Status") or {}).get("select") or {}
    return {o["name"] for o in select.get("options", [])}


def query_due():
    """Every page that is Approved and whose scheduled moment has passed."""
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = notion_request(
        "POST",
        f"{NOTION_API}/databases/{need('NOTION_DATABASE_ID')}/query",
        json={
            "filter": {
                "and": [
                    {"property": "Status", "select": {"equals": APPROVED}},
                    {"property": "Scheduled date", "date": {"on_or_before": now_iso}},
                ]
            },
            "sorts": [{"property": "Scheduled date", "direction": "ascending"}],
            "page_size": 25,
        },
    )
    items = []
    for page in body.get("results", []):
        props = page.get("properties", {})
        slug = plain_text(props.get("Name"))
        if not slug:
            continue
        items.append(
            {
                "page_id": page["id"],
                "slug": slug,
                "caption": plain_text(props.get("Caption")),
                "slide_count": (props.get("Slide count") or {}).get("number") or 0,
                "scheduled": ((props.get("Scheduled date") or {}).get("date") or {}).get("start"),
            }
        )
    return items


def count_posted_today():
    today = datetime.now(timezone.utc).date().isoformat()
    body = notion_request(
        "POST",
        f"{NOTION_API}/databases/{need('NOTION_DATABASE_ID')}/query",
        json={
            "filter": {
                "and": [
                    {"property": "Status", "select": {"equals": POSTED}},
                    {"property": "Scheduled date", "date": {"on_or_after": today}},
                ]
            },
            "page_size": 100,
        },
    )
    return len(body.get("results", []))


def set_status(page_id, status, attempts=4):
    """Write a Status back. Retried, because this write is the double post lock."""
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            notion_request(
                "PATCH",
                f"{NOTION_API}/pages/{page_id}",
                json={"properties": {"Status": {"select": {"name": status}}}},
            )
            return True
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
            log(f"  could not set Status to {status} (attempt {attempt}): {exc}")
            time.sleep(3 * attempt)
    log(f"  WARNING: Status was never set to {status}. Last error: {last}")
    return False


# ------------------------------------------------------------------ images --


def github_branch(repo):
    headers = {"Accept": "application/vnd.github+json"}
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(f"https://api.github.com/repos/{repo}", headers=headers, timeout=30)
    if resp.status_code != 200:
        die(f"cannot read the image repo '{repo}': {resp.status_code} {resp.text[:200]}")
    return resp.json().get("default_branch", "main")


def image_urls(repo, branch, slug, count):
    return [
        f"https://raw.githubusercontent.com/{repo}/{branch}/{slug}/{n:02d}.jpg"
        for n in range(1, count + 1)
    ]


def verify_public(url, attempts=6):
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, timeout=30, stream=True)
            ctype = resp.headers.get("Content-Type", "")
            ok = resp.status_code == 200 and "image/jpeg" in ctype
            resp.close()
            if ok:
                return True
            log(f"  not ready ({resp.status_code} {ctype}), attempt {attempt}")
        except Exception as exc:  # noqa: BLE001
            log(f"  fetch failed ({exc}), attempt {attempt}")
        time.sleep(4)
    return False


# ------------------------------------------------------------------- graph --


TOKEN_HELP = """The Instagram access token has expired or been revoked. Get a new one:
  1. developers.facebook.com/tools/explorer, pick your app, generate a User
     Token with instagram_basic, instagram_content_publish and
     pages_read_engagement
  2. developers.facebook.com/tools/debug/accesstoken, paste it in, then click
     Extend Access Token at the bottom for the 60 day version
  3. Put it in IG_ACCESS_TOKEN in the cloud environment and in the .env on the
     machine that renders, and set IG_TOKEN_ISSUED to today
Nothing will post until that is done."""


def graph_post(path, data):
    resp = requests.post(f"{GRAPH}/{path}", data=data, timeout=120)
    body = resp.json() if resp.content else {}
    if resp.status_code != 200 or "id" not in body:
        error = body.get("error") or {}
        # 190 is the whole family of expired and revoked token errors. It is by
        # far the most common way this runner fails, and the raw Graph message
        # does not say what to do about it.
        if error.get("code") == 190:
            message = error.get("message", "token rejected")
            raise RuntimeError(message + "\n" + TOKEN_HELP)
        raise RuntimeError(f"Graph POST {path} failed: {resp.status_code} {json.dumps(body)[:300]}")
    return body["id"]


def check_token():
    """A read only call, so it proves the token without posting anything."""
    ig_user = need("IG_USER_ID")
    token = need("IG_ACCESS_TOKEN")
    resp = requests.get(
        f"{GRAPH}/{ig_user}",
        params={"fields": "id,username", "access_token": token},
        timeout=30,
    )
    body = resp.json() if resp.content else {}
    if resp.status_code == 200 and body.get("id"):
        log(f"token OK. Connected to @{body.get('username', 'unknown')} ({body['id']}).")
        return True
    error = body.get("error") or {}
    if error.get("code") == 190:
        log("token REJECTED.")
        print(TOKEN_HELP)
    else:
        log(f"token check failed: {resp.status_code} {json.dumps(body)[:300]}")
    return False


def token_age_warning():
    """Warn before the 60 day token dies, rather than after."""
    issued = (os.environ.get("IG_TOKEN_ISSUED") or "").strip()
    if not issued:
        return
    try:
        when = datetime.strptime(issued, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        log(f"IG_TOKEN_ISSUED is {issued!r}, not YYYY-MM-DD, so token age is unknown")
        return
    days = (datetime.now(timezone.utc) - when).days
    if days >= 50:
        log(f"WARNING: the Instagram token is {days} days old and dies at 60. Refresh it.")


def wait_finished(container_id, token, timeout_seconds=180):
    deadline = time.time() + timeout_seconds
    last = ""
    while time.time() < deadline:
        resp = requests.get(
            f"{GRAPH}/{container_id}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=30,
        )
        body = resp.json() if resp.content else {}
        last = body.get("status_code") or json.dumps(body)[:150]
        if last == "FINISHED":
            return
        if last == "ERROR":
            raise RuntimeError(f"container {container_id} reported ERROR: {json.dumps(body)[:300]}")
        time.sleep(5)
    raise RuntimeError(f"container {container_id} never reached FINISHED. Last: {last}")


DOWNLOAD_FAILED_SUBCODE = 2207052
ITEM_ATTEMPTS = 4


def item_container_with_retry(ig_user, token, url):
    """Create one carousel item, retrying when Meta fails to download the image.

    Meta sometimes reports a slide it could not fetch from GitHub as
    "Only photo or video can be accepted" (code 9004, subcode 2207052), even
    though the same file is fine and the slides before it were accepted. It is
    a failed download, not a bad file, so wait and ask again. Each retry adds a
    query string, which GitHub ignores, so Meta does not reuse a cached failure.
    """
    for attempt in range(1, ITEM_ATTEMPTS + 1):
        target = url if attempt == 1 else f"{url}?try={attempt}"
        resp = requests.post(
            f"{GRAPH}/{ig_user}/media",
            data={"image_url": target, "is_carousel_item": "true", "access_token": token},
            timeout=120,
        )
        body = resp.json() if resp.content else {}
        if resp.status_code == 200 and "id" in body:
            if attempt > 1:
                log(f"  {url.rsplit('/', 1)[-1]} accepted on attempt {attempt}")
            return body["id"]

        error = body.get("error") or {}
        if error.get("code") == 190:
            raise RuntimeError(error.get("message", "token rejected") + "\n" + TOKEN_HELP)

        download_failed = error.get("error_subcode") == DOWNLOAD_FAILED_SUBCODE
        if not download_failed or attempt == ITEM_ATTEMPTS:
            raise RuntimeError(
                f"Graph POST {ig_user}/media failed for {url.rsplit('/', 1)[-1]} "
                f"after {attempt} attempt(s): {resp.status_code} {json.dumps(body)[:300]}"
            )
        wait = 10 * attempt
        log(f"  Meta could not download {url.rsplit('/', 1)[-1]}, retrying in {wait}s "
            f"(attempt {attempt} of {ITEM_ATTEMPTS})")
        time.sleep(wait)


def post_carousel(item, urls, dry_run):
    ig_user = need("IG_USER_ID")
    token = need("IG_ACCESS_TOKEN")

    if dry_run:
        log(f"  dry run: would build {len(urls)} containers and publish")
        return None

    children = []
    for url in urls:
        cid = item_container_with_retry(ig_user, token, url)
        wait_finished(cid, token)
        children.append(cid)
        log(f"  container {cid} for {url.rsplit('/', 1)[-1]}")
        # A short gap between fetches. Meta pulls every slide from GitHub in
        # quick succession, and a burst is when its download fails.
        time.sleep(2)

    creation_id = graph_post(
        f"{ig_user}/media",
        {
            "media_type": "CAROUSEL",
            "children": ",".join(children),
            "caption": item["caption"],
            "access_token": token,
        },
    )
    wait_finished(creation_id, token)
    log(f"  carousel container {creation_id}")

    media_id = graph_post(
        f"{ig_user}/media_publish",
        {"creation_id": creation_id, "access_token": token},
    )
    return media_id


# -------------------------------------------------------------------- main --


def handle(item, repo, branch, options, dry_run):
    slug = item["slug"]
    log(f"{slug}: due at {item['scheduled']}, Approved")

    count = item["slide_count"]
    if not count or count > MAX_CHILDREN:
        raise RuntimeError(
            f"slide count is {count!r}. It must be between 1 and {MAX_CHILDREN}. "
            f"Fix the Slide count property in Notion."
        )
    if not item["caption"].strip():
        raise RuntimeError("the Caption property is empty in Notion.")

    urls = image_urls(repo, branch, slug, count)
    log(f"  checking {len(urls)} public images")
    for url in urls:
        if not verify_public(url):
            raise RuntimeError(
                f"{url} is not reachable as image/jpeg. The slides were probably "
                f"never pushed. Run: python scripts/publish.py {slug} --cloud --at ..."
            )

    # Take the lock first when the workspace has a Posting option, so a crash
    # midway cannot leave the page Approved and due for a second attempt.
    if POSTING in options and not dry_run:
        set_status(item["page_id"], POSTING, attempts=2)

    media_id = post_carousel(item, urls, dry_run)

    if dry_run:
        log(f"  dry run: {slug} left as is")
        return False

    set_status(item["page_id"], POSTED)
    log(f"{slug}: PUBLISHED, media id {media_id}")
    return True


def main():
    ap = argparse.ArgumentParser(description="Post approved, due carousels.")
    ap.add_argument("--dry-run", action="store_true",
                    help="check everything, call no Graph API write")
    ap.add_argument("--check-token", action="store_true",
                    help="say whether the Instagram token still works, then stop")
    args = ap.parse_args()

    load_local_env()

    token_age_warning()

    if args.check_token:
        check_token()
        return

    repo = normalize_repo(need("GITHUB_REPO"))
    branch = github_branch(repo)
    log(f"image host: {repo} on {branch}")

    try:
        options = status_options()
    except Exception as exc:  # noqa: BLE001
        die(f"cannot read the Notion database: {exc}")

    for required in (APPROVED, POSTED, FAILED):
        if required not in options:
            die(f"the Status property has no '{required}' option. Add it in Notion.")
    if POSTING not in options:
        log(f"note: no '{POSTING}' status option. Add one for a safer lock.")

    due = query_due()
    if not due:
        log("nothing approved and due. Done.")
        return

    log(f"{len(due)} carousel(s) approved and due")

    posted_today = count_posted_today()
    published = 0

    for item in due:
        if posted_today + published >= DAILY_PUBLISH_CAP and not args.dry_run:
            log(f"stopping at the daily cap of {DAILY_PUBLISH_CAP}")
            break
        try:
            if handle(item, repo, branch, options, args.dry_run):
                published += 1
        except Exception as exc:  # noqa: BLE001
            log(f"{item['slug']}: FAILED, {exc}")
            if not args.dry_run:
                set_status(item["page_id"], FAILED)

    log(f"done. published {published} this run.")


if __name__ == "__main__":
    main()
