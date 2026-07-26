#!/usr/bin/env python3
"""
Auto-update script for the ungoogled-chromium-void package template.

What it does
------------
1. Reads the current `version` and `revision` from the xbps-src template.
2. Queries ChromiumDash for the latest stable Chromium release.
3. Verifies that an `ungoogled-software/ungoogled-chromium` tag exists for the
   new version (tries revisions 1..5 so we also pick up re-releases).
4. Verifies that a `chromium-linux-tarballs/chromium-tarballs` release exists
   for the new version (this is where the template fetches the chromium
   source tarball from).
5. If everything is in place, it rewrites:
      * `version=` and `revision=` in both template files
      * `checksum=` in both template files (downloads every distfile and
        computes sha256 locally — equivalent to `xgensum -f`)
      * the VERSION-INFO block in README.md
6. Emits GitHub Actions outputs (`updated`, `new_version`, `new_revision`)
   so the calling workflow can decide whether to commit.

Exit codes
----------
  0  OK — either an update was produced, or no update was needed yet.
  1  Error — could not reach an upstream, or a distfile download failed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

# This file lives at <repo-root>/.github/scripts/update_package.py
# parents[0]=scripts  parents[1]=.github  parents[2]=<repo-root>
REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = [
    REPO_ROOT / "void-packages/srcpkgs/ungoogled-chromium/template",
    REPO_ROOT / "void-packages/srcpkgs/ungoogled-chromium-qt6/template",
]
README = REPO_ROOT / "README.md"

CHROMIUMDASH_URL = (
    "https://chromiumdash.appspot.com/fetch_releases"
    "?channel=Stable&platform=Linux&num=1&offset=0"
)
UC_RELEASE_TAG_API = (
    "https://api.github.com/repos/ungoogled-software/ungoogled-chromium"
    "/releases/tags/{ver}-{rev}"
)
TARBALL_RELEASE_TAG_API = (
    "https://api.github.com/repos/chromium-linux-tarballs/chromium-tarballs"
    "/releases/tags/{ver}"
)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
USER_AGENT = "ungoogled-chromium-void-auto-update/1.0 (+github-actions)"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _headers(accept: str = "application/json") -> dict:
    h = {
        "Accept": accept,
        "User-Agent": USER_AGENT,
    }
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def fetch_text(url: str, accept: str = "application/json", timeout: int = 60) -> str | None:
    """GET a URL and return its body as text. Returns None on 404."""
    req = urllib.request.Request(url, headers=_headers(accept))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        print(f"::error::HTTP {e.code} fetching {url}: {e.reason}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"::error::URL error fetching {url}: {e}", file=sys.stderr)
        return None


def stream_sha256(url: str, timeout: int = 1800) -> str:
    """Stream-download a URL and compute its sha256 without buffering it all."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        while True:
            chunk = resp.read(1 << 20)  # 1 MiB
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def set_output(name: str, value: str) -> None:
    """Write a GitHub Actions output (modern $GITHUB_OUTPUT file)."""
    out_file = os.environ.get("GITHUB_OUTPUT")
    if out_file:
        with open(out_file, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    print(f"::set-output name={name}::{value}")  # legacy fallback (no-op on new runners)


# ---------------------------------------------------------------------------
# Version probing
# ---------------------------------------------------------------------------

def read_current_version() -> tuple[str, str, str]:
    """Return (version, revision, _rollup) from the main template."""
    text = TEMPLATES[0].read_text(encoding="utf-8")
    ver = re.search(r"^version=(\S+)", text, re.MULTILINE)
    rev = re.search(r"^revision=(\d+)", text, re.MULTILINE)
    rollup = re.search(r"^_rollup=(\S+)", text, re.MULTILINE)
    if not (ver and rev and rollup):
        raise RuntimeError("Could not parse version/revision/_rollup from template")
    return ver.group(1), rev.group(1), rollup.group(1)


def read_commit_id() -> str:
    text = TEMPLATES[0].read_text(encoding="utf-8")
    m = re.search(r'^_commit_id="([^"]*)"', text, re.MULTILINE)
    return m.group(1) if m else ""


def latest_chromium_stable() -> str | None:
    body = fetch_text(CHROMIUMDASH_URL)
    if not body:
        return None
    # The endpoint returns a JSON list of release objects.
    try:
        data = json.loads(body)
        if isinstance(data, list) and data:
            return data[0].get("version")
    except json.JSONDecodeError:
        pass
    # Fallback: regex (matches the `update` file pattern)
    m = re.search(r'"version":\s*"([^"]+)"', body)
    return m.group(1) if m else None


def find_ungoogled_revision(version: str) -> str | None:
    """Find the highest ungoogled-chromium revision (1..5) for this chromium version."""
    for rev in range(1, 6):
        url = UC_RELEASE_TAG_API.format(ver=version, rev=rev)
        if fetch_text(url) is not None:
            return str(rev)
    return None


def tarball_release_exists(version: str) -> bool:
    return fetch_text(TARBALL_RELEASE_TAG_API.format(ver=version)) is not None


# ---------------------------------------------------------------------------
# Template rewriting
# ---------------------------------------------------------------------------

def build_checksum_field(sha_list: list[str]) -> str:
    """Format a multi-line `checksum="..."` block matching the template style."""
    # Original style: first hash on the same line, continuation lines indented
    # by a single space, no trailing backslash (shell sees one quoted string).
    body = "\n ".join(sha_list)
    return f'checksum="{body}"'


def rewrite_template(path: Path, new_ver: str, new_rev: str, sha_list: list[str]) -> None:
    text = path.read_text(encoding="utf-8")

    text = re.sub(r"^version=.*$", f"version={new_ver}", text, count=1, flags=re.MULTILINE)
    text = re.sub(r"^revision=.*$", f"revision={new_rev}", text, count=1, flags=re.MULTILINE)

    new_checksum = build_checksum_field(sha_list)
    # `[^"]*` is safe: sha256 hex never contains a `"`.
    text = re.sub(r'checksum="[^"]*"', new_checksum, text, count=1, flags=re.DOTALL)

    path.write_text(text, encoding="utf-8")


def rewrite_readme(new_ver: str, new_rev: str) -> None:
    text = README.read_text(encoding="utf-8")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    new_block = (
        "| **Component**                                 | **Version**        |\n"
        "|-----------------------------------------------|--------------------|\n"
        "| **[Chromium (google)](https://chromium.googlesource.com/chromium/src)**"
        "                           | `" + new_ver + "` |\n"
        "| **[ungoogled-chromium (ungoogled-software)]"
        "(https://github.com/ungoogled-software/ungoogled-chromium)**"
        "                          | `" + new_ver + "` |\n"
        "| **[ungoogled-chromium-void (KevinRunforrestt)]"
        "(https://github.com/KevinRunforrestt/ungoogled-chromium-void)**"
        "                             | `" + new_ver + "_" + new_rev + "` |\n"
        "\n"
        "<sub>***Updated: " + now + "***</sub>\n"
    )

    pattern = r"(<!-- VERSION-INFO-START -->\n)(.*?)(<!-- VERSION-INFO-END -->)"
    replaced, n = re.subn(pattern, lambda m: m.group(1) + new_block + m.group(3),
                          text, count=1, flags=re.DOTALL)
    if n == 0:
        print("::warning::VERSION-INFO markers not found in README.md — skipping README update")
        return
    README.write_text(replaced, encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=== ungoogled-chromium-void auto-update ===")

    cur_ver, cur_rev, rollup = read_current_version()
    print(f"Current template:  version={cur_ver}  revision={cur_rev}  rollup={rollup}")

    commit_id = read_commit_id()
    if commit_id:
        print(f"::warning::_commit_id is set ('{commit_id}'); the ungoogled-chromium "
              "distfile URL will use this commit instead of a release tag. The auto-"
              "updater will still bump version+checksums based on the ungoogled-chromium "
              "release tag, which may not match. Manual review recommended.")

    latest_ver = latest_chromium_stable()
    if not latest_ver:
        print("::error::Could not determine latest Chromium stable version")
        return 1
    print(f"Latest Chromium stable: {latest_ver}")

    # Decide whether we need to do anything.
    if latest_ver == cur_ver:
        # Same chromium version — check if a newer ungoogled-chromium revision exists.
        new_uc_rev = find_ungoogled_revision(latest_ver)
        if not new_uc_rev:
            print(f"ungoogled-chromium tag for {latest_ver} not found yet; nothing to do.")
            set_output("updated", "false")
            return 0
        if new_uc_rev == cur_rev:
            print(f"No update needed — still at {cur_ver}_{cur_rev}.")
            set_output("updated", "false")
            return 0
        print(f"New ungoogled-chromium revision detected: {cur_rev} -> {new_uc_rev}")
        new_ver, new_rev = latest_ver, new_uc_rev
    else:
        new_ver = latest_ver
        new_rev = find_ungoogled_revision(new_ver)
        if not new_rev:
            print(f"ungoogled-chromium has not tagged {new_ver} yet; will retry next run.")
            set_output("updated", "false")
            return 0

    # Make sure the chromium source tarball exists for the new version.
    if not tarball_release_exists(new_ver):
        print(f"chromium-linux-tarballs has no release for {new_ver} yet; will retry next run.")
        set_output("updated", "false")
        return 0

    # Build the list of distfile URLs exactly as the template does.
    distfiles = [
        f"https://github.com/chromium-linux-tarballs/chromium-tarballs/releases/"
        f"download/{new_ver}/chromium-{new_ver}-linux.tar.xz",
        f"https://github.com/ungoogled-software/ungoogled-chromium/archive/"
        f"refs/tags/{new_ver}-{new_rev}.tar.gz",
        f"https://registry.npmjs.org/@rollup/wasm-node/-/wasm-node-{rollup}.tgz",
    ]

    print(f"Updating to {new_ver}_{new_rev}; downloading {len(distfiles)} distfiles for sha256…")
    sha_list: list[str] = []
    for url in distfiles:
        print(f"  -> {url}")
        sha = stream_sha256(url)
        if not sha:
            print(f"::error::Failed to download {url}")
            return 1
        print(f"     sha256={sha}")
        sha_list.append(sha)

    # Apply changes to both template copies.
    for tpl in TEMPLATES:
        rewrite_template(tpl, new_ver, new_rev, sha_list)
        print(f"Updated template: {tpl.relative_to(REPO_ROOT)}")

    rewrite_readme(new_ver, new_rev)
    print(f"Updated README: {README.relative_to(REPO_ROOT)}")

    set_output("updated", "true")
    set_output("new_version", new_ver)
    set_output("new_revision", new_rev)
    print(f"Done. Bumped ungoogled-chromium to {new_ver}_{new_rev}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
