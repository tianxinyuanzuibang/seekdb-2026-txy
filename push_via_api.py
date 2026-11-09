#!/usr/bin/env python3
"""Push the working tree to GitHub using the REST API.

The sandbox cannot reach github.com over the git protocol (the HTTPS
connection is reset), but the REST API is reachable.  This script mirrors
the local tree into a branch by creating a git blob, tree and commit for
every file and then updating the branch reference.

    python3 push_via_api.py [--branch 2026-competition-round2] [--message ...]
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

TOKEN = os.environ.get("MVD_GITHUB_TOKEN", "")
if not TOKEN:
    sys.exit("set MVD_GITHUB_TOKEN to a GitHub token with repo scope")
REPO = os.environ.get("MVD_GITHUB_REPO", "tianxinyuanzuibang/seekdb-2026-txy")
ROOT = os.path.dirname(os.path.abspath(__file__))
API = "https://api.github.com/repos/" + REPO


def api(url, method="GET", data=None, raw=False):
    body = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", "Bearer " + TOKEN)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "minivectordb-push")
    if body:
        request.add_header("Content-Type", "application/json")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                payload = response.read()
                if raw:
                    return response.status, payload
                return response.status, json.loads(payload.decode() or "{}")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode()
            try:
                parsed = json.loads(payload or "{}")
            except ValueError:
                parsed = {"raw": payload}
            if exc.code in (502, 503, 504) and attempt < 4:
                continue
            return exc.code, parsed
        except urllib.error.URLError:
            if attempt < 4:
                continue
            raise
    raise RuntimeError("unreachable")


def local_files():
    """Every tracked file of the working tree, as (path, bytes)."""
    output = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode()
    files = []
    for relative in output.splitlines():
        relative = relative.strip()
        if not relative:
            continue
        full = os.path.join(ROOT, relative)
        if not os.path.isfile(full):
            continue
        with open(full, "rb") as handle:
            files.append((relative, handle.read()))
    return files


def upload_blob(data):
    encoded = base64.b64encode(data).decode()
    status, payload = api(
        API + "/git/blobs", "POST", {"content": encoded, "encoding": "base64"}
    )
    if status not in (200, 201):
        raise RuntimeError("blob upload failed: {}".format(payload))
    return payload["sha"]


def build_tree(entries, base_tree, deleted=None):
    """Create a tree from (path, mode, type, sha) entries.

    ``deleted`` lists the paths that must be removed from the base tree.
    """
    payload = {
        "base_tree": base_tree,
        "tree": [
            {
                "path": path,
                "mode": mode,
                "type": type_,
                "sha": sha,
            }
            for path, mode, type_, sha in entries
        ],
    }
    for path in deleted or ():
        payload["tree"].append({
            "path": path,
            "mode": "100644",
            "type": "blob",
            "sha": None,          # null sha removes the path
        })
    status, result = api(API + "/git/trees", "POST", payload)
    if status not in (200, 201):
        raise RuntimeError("tree creation failed: {}".format(result))
    return result["sha"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--branch", default="2026-competition-round2")
    parser.add_argument("--message", default="update MiniVectorDB")
    parser.add_argument(
        "--parent", default=None,
        help="commit SHA to use as the parent (defaults to the branch head)",
    )
    args = parser.parse_args()

    files = local_files()
    print("pushing {} file(s) to {}".format(len(files), args.branch))

    # 1. resolve the parent commit and the base tree
    status, ref = api(
        API + "/git/ref/heads/" + urllib.parse.quote(args.branch, safe="")
    )
    if status == 200:
        parent = args.parent or ref["object"]["sha"]
        status, commit = api(API + "/git/commits/" + parent)
        if status != 200:
            raise RuntimeError("parent lookup failed: {}".format(commit))
        base_tree = commit["tree"]["sha"]
    else:
        parent = args.parent
        base_tree = None
        print("branch does not exist yet - creating it")

    # 2. upload every blob
    entries = []
    for relative, data in files:
        sha = upload_blob(data)
        entries.append((relative, "100644", "blob", sha))
        print("  blob {} {}".format(sha[:8], relative))

    # 3. build the tree
    #    Deletions have to be expressed explicitly: the tree is created on
    #    top of the previous commit's tree, so a file that was deleted
    #    locally would otherwise survive on the remote.
    deleted = []
    if parent:
        status, previous = api(
            API + "/git/trees/" + base_tree + "?recursive=1"
        )
        if status == 200:
            previous_paths = {
                entry["path"] for entry in previous.get("tree", [])
                if entry.get("type") == "blob"
            }
            current_paths = {path for path, _mode, _type, _sha in entries}
            deleted = sorted(previous_paths - current_paths)
            for path in deleted:
                print("  delete {}".format(path))
    tree_sha = build_tree(entries, base_tree, deleted)
    print("tree {}".format(tree_sha))

    # 4. create the commit
    commit_payload = {
        "message": args.message,
        "tree": tree_sha,
    }
    if parent:
        commit_payload["parents"] = [parent]
    status, commit = api(API + "/git/commits", "POST", commit_payload)
    if status not in (200, 201):
        raise RuntimeError("commit failed: {}".format(commit))
    commit_sha = commit["sha"]
    print("commit {}".format(commit_sha))

    # 5. move the branch (create it when it does not exist yet)
    update = {
        "sha": commit_sha,
        "force": True,
    }
    quoted = urllib.parse.quote(args.branch, safe="")
    status, result = api(API + "/git/refs/heads/" + quoted, "PATCH", update)
    if status not in (200, 201):
        # The branch does not exist yet - create it.
        status, result = api(
            API + "/git/refs", "POST",
            {"ref": "refs/heads/" + args.branch, "sha": commit_sha},
        )
    if status not in (200, 201):
        raise RuntimeError("ref update failed: {}".format(result))
    print("branch {} -> {}".format(args.branch, commit_sha))
    print("full SHA: {}".format(commit_sha))
    return 0


if __name__ == "__main__":
    import urllib.parse
    sys.exit(main())
