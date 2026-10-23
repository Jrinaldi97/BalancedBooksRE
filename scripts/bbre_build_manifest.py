#!/usr/bin/env python3
"""Bind a local BalancedBooksRE package to a clean, exact source commit."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess


def manifest(root, expected_sha=None):
    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(root), *args], text=True
        ).strip()

    commit = git("rev-parse", "HEAD")
    if expected_sha and commit != expected_sha:
        raise ValueError("The checkout does not match the expected commit")
    if git("status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("Commit or remove build inputs before packaging a release")
    app = root / "app"
    digest = sha256()
    paths = sorted(app.rglob("*.py"))
    if not paths:
        raise ValueError("No application source files found")
    for path in paths:
        digest.update(path.relative_to(app).as_posix().encode() + b"\0")
        digest.update(sha256(path.read_bytes()).digest())
    return {
        "source_commit": commit,
        "source_tree_sha256": digest.hexdigest(),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "dirty": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--expected-sha")
    args = parser.parse_args()
    result = manifest(args.root, args.expected_sha)
    target = args.root / "app/portfolio/build_manifest.json"
    target.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
