#!/usr/bin/env python3
"""
Deploy dashboard to Cloudflare Pages via Direct Upload API.

Requires:
  - CLOUDFLARE_API_TOKEN env var (or in .env file)
  - Account ID configured below

Usage:
  python3 scripts/deploy_cloudflare.py
"""

import hashlib
import json
import mimetypes
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("Installing requests...")
    os.system(f"{sys.executable} -m pip install requests -q")
    import requests

ACCOUNT_ID = "2b75c6fb3ad4b0b6c46408ffc96366a0"
PROJECT_NAME = "chanlun-dashboard"
BASE_URL = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/pages/projects"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = PROJECT_ROOT / "reports"
ENV_FILE = PROJECT_ROOT / ".env"


def load_token() -> str:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not token and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("CLOUDFLARE_API_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not token:
        print("ERROR: CLOUDFLARE_API_TOKEN not set.")
        print(f"Set it in environment or add to {ENV_FILE}:")
        print('  CLOUDFLARE_API_TOKEN=your_token_here')
        sys.exit(1)
    return token


def get_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
    }


def ensure_project(token: str) -> bool:
    """Create project if not exists. Returns True if ready."""
    headers = get_headers(token)
    r = requests.get(f"{BASE_URL}/{PROJECT_NAME}", headers=headers)
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        print(f"Creating project '{PROJECT_NAME}'...")
        payload = {
            "name": PROJECT_NAME,
            "production_branch": "main",
        }
        r = requests.post(BASE_URL, headers=headers, json=payload)
        if r.status_code in (200, 201):
            print(f"Project created: {PROJECT_NAME}")
            return True
        else:
            print(f"Failed to create project: {r.status_code} {r.text}")
            return False
    print(f"Error checking project: {r.status_code} {r.text}")
    return False


def collect_files(deploy_dir: Path):
    """Collect all files to deploy (relative path, absolute path)."""
    files = []
    for p in sorted(deploy_dir.rglob("*")):
        if p.is_file():
            rel = p.relative_to(deploy_dir).as_posix()
            files.append((rel, p))
    return files


def compute_manifest(files):
    """Compute a hash of all file contents for change detection."""
    h = hashlib.md5()
    for rel, path in files:
        h.update(rel.encode())
        h.update(path.read_bytes()[:4096])
    return h.hexdigest()


def deploy(token: str):
    """Deploy files via Direct Upload. Returns deployment URL or None."""
    headers = get_headers(token)
    files = collect_files(DEPLOY_DIR)
    if not files:
        print("No files to deploy!")
        return None

    total_size = sum(p.stat().st_size for _, p in files)
    print(f"Deploying {len(files)} files ({total_size / 1024:.0f} KB) to {PROJECT_NAME}...")

    deploy_url = f"{BASE_URL}/{PROJECT_NAME}/deployments"

    multipart_files = []
    for rel, path in files:
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        multipart_files.append(
            (rel, (rel, path.open("rb"), content_type))
        )

    t0 = time.time()
    r = requests.post(deploy_url, headers=headers, files=multipart_files)
    elapsed = time.time() - t0

    for _, (_, fh, _) in multipart_files:
        fh.close()

    if r.status_code in (200, 201):
        data = r.json()
        result = data.get("result", {})
        url = result.get("url", "")
        deploy_id = result.get("id", "")
        print(f"Deploy OK in {elapsed:.1f}s")
        print(f"  ID: {deploy_id}")
        print(f"  URL: {url}")
        return url
    else:
        print(f"Deploy FAILED: {r.status_code}")
        try:
            err = r.json()
            for e in err.get("errors", []):
                print(f"  {e.get('code')}: {e.get('message')}")
        except Exception:
            print(f"  {r.text[:500]}")
        return None


def main():
    token = load_token()
    if not ensure_project(token):
        sys.exit(1)
    url = deploy(token)
    if url:
        print(f"\n Dashboard live at: {url}")
        prod_url = f"https://{PROJECT_NAME}.pages.dev"
        print(f" Production URL: {prod_url}")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
