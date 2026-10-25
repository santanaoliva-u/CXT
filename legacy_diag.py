#!/usr/bin/env python3
"""Read-only diagnostic for legacy multi-photo items in autoqueue.json.

Never publishes, never contacts the daemon, never writes to the queue.
"""

import json
import os
import re
import sys

QUEUE = os.path.join(
    os.environ.get("CXT_DIR", os.path.expanduser("~/.cxt")),
    "autoqueue.json",
)

# photo/?fbid=<id>&set=pcb.<id> or photo/?fbid=<id>&set=gm.<id>
PHOTO_RE = re.compile(r"photo/?\?")
POSTS_RE = re.compile(r"/posts/(\d+)")
GROUPS_POSTS_RE = re.compile(r"/groups/([^/?#]+)/posts/(\d+)")
PERMALINK_RE = re.compile(r"/permalink\.php")
SET_PCB_RE = re.compile(r"^pcb\.(\d+)$")
SET_GM_RE = re.compile(r"^gm\.(\d+)$")


def _query(url):
    """Extract query parameters without urllib (stdlib-only, no decoding)."""
    params = {}
    for key, value in re.findall(r"[?&]([^=&#]+)=([^&#]*)", url):
        if key not in params:
            params[key] = value
    return params


def parse_url(url):
    """Return {fbid, set, id, pcb, post_id, kind} for a Facebook URL."""
    info = {
        "fbid": None,
        "set": None,
        "id": None,
        "pcb": None,
        "post_id": None,
        "kind": "other",
    }
    if not isinstance(url, str):
        return info

    query = _query(url)
    info["fbid"] = query.get("fbid")
    info["set"] = query.get("set")
    info["id"] = query.get("id")

    if info["set"]:
        match = SET_PCB_RE.match(info["set"])
        if match:
            info["pcb"] = match.group(1)

    group_match = GROUPS_POSTS_RE.search(url)
    if group_match:
        info["kind"] = "post-permalink"
        info["post_id"] = group_match.group(2)
        return info

    posts_match = POSTS_RE.search(url)
    if posts_match:
        info["kind"] = "post-permalink"
        info["post_id"] = posts_match.group(1)
        return info

    if PERMALINK_RE.search(url) and query.get("story_fbid"):
        info["kind"] = "post-permalink"
        info["post_id"] = query["story_fbid"]
        return info

    if PHOTO_RE.search(url):
        info["kind"] = "photo-viewer"
        if info["pcb"]:
            # A pcb.<id> token is the post id; it encodes no owner or group.
            info["post_id"] = info["pcb"]
        return info

    return info


def recommend(info):
    if info["kind"] == "post-permalink":
        return "ok (post-level)"
    value = info.get("set") or ""
    if SET_GM_RE.match(value):
        return "needs gid; re-select or use /groups/<gid>/posts/<id>/"
    if SET_PCB_RE.match(value):
        return "needs owner+gid; re-select the post in the UI"
    return "unresolved; re-select manually"


def _load_queue(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle), None
    except FileNotFoundError:
        return None, "missing"
    except (json.JSONDecodeError, ValueError):
        return None, "invalid"


def main():
    data, error = _load_queue(QUEUE)
    if error == "missing":
        print("queue_missing")
        return 0
    if error == "invalid":
        print("queue_invalid")
        return 1

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        items = []

    legacy = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        info = parse_url(url)
        value = info.get("set") or ""
        is_legacy = (
            isinstance(url, str)
            and "photo/?" in url
            and bool(SET_PCB_RE.match(value) or SET_GM_RE.match(value))
        )
        if is_legacy:
            legacy.append((index, url, info))

    print("queue=%s" % QUEUE)
    print("items_total=%d" % len(items))
    print("legacy_items=%d" % len(legacy))

    for index, url, info in legacy:
        value = info.get("set") or ""
        print("---")
        print("index=%d" % index)
        print("url=%s" % url)
        print("fbid=%s" % (info.get("fbid") or "-"))
        print("set=%s" % (value or "-"))
        print("pcb=%s" % (info.get("pcb") or "-"))
        print("post_id=%s" % (info.get("post_id") or "-"))
        print("post_level=%s" % ("yes" if info["kind"] == "post-permalink" else "no"))
        print("kind=%s" % info["kind"])
        print("recommend=%s" % recommend(info))

    return 0


if __name__ == "__main__":
    sys.exit(main())
