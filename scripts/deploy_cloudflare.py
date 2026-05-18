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


def load_credentials():
    """Load Cloudflare credentials from env or .env file.
    Supports both API Token (Bearer) and Global API Key (email+key).
    Returns dict with auth headers.
    """
    env_vars = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip().strip('"').strip("'")

    api_token = os.environ.get("CLOUDFLARE_API_TOKEN", env_vars.get("CLOUDFLARE_API_TOKEN", ""))
    if api_token:
        return {"Authorization": f"Bearer {api_token}"}

    api_key = os.environ.get("CLOUDFLARE_API_KEY", env_vars.get("CLOUDFLARE_API_KEY", ""))
    email = os.environ.get("CLOUDFLARE_EMAIL", env_vars.get("CLOUDFLARE_EMAIL", ""))
    if api_key and email:
        return {"X-Auth-Key": api_key, "X-Auth-Email": email}

    print("ERROR: No Cloudflare credentials found.")
    print(f"Set in environment or {ENV_FILE}:")
    print("  Option A: CLOUDFLARE_API_TOKEN=your_token")
    print("  Option B: CLOUDFLARE_API_KEY=your_key + CLOUDFLARE_EMAIL=your_email")
    sys.exit(1)


def get_headers():
    return load_credentials()


def ensure_project(headers) -> bool:
    """Create project if not exists. Returns True if ready."""
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


def deploy(headers):
    """Deploy files via Direct Upload with manifest. Returns deployment URL or None."""
    files = collect_files(DEPLOY_DIR)
    if not files:
        print("No files to deploy!")
        return None

    total_size = sum(p.stat().st_size for _, p in files)
    print(f"Deploying {len(files)} files ({total_size / 1024:.0f} KB) to {PROJECT_NAME}...")

    deploy_url = f"{BASE_URL}/{PROJECT_NAME}/deployments"

    # Build manifest: {"/<path>": "<sha256_hex>", ...}
    # and collect file data keyed by hash
    manifest = {}
    file_data_by_hash = {}
    for rel, path in files:
        content = path.read_bytes()
        file_hash = hashlib.sha256(content).hexdigest()
        key = "/" + rel
        manifest[key] = file_hash
        if file_hash not in file_data_by_hash:
            content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            file_data_by_hash[file_hash] = (content, content_type)

    # Multipart: "manifest" field + each file keyed by its hash
    multipart = [("manifest", (None, json.dumps(manifest), "application/json"))]
    for fhash, (content, ctype) in file_data_by_hash.items():
        multipart.append((fhash, (fhash, content, ctype)))

    t0 = time.time()
    r = requests.post(deploy_url, headers=headers, files=multipart)
    elapsed = time.time() - t0

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
    headers = get_headers()
    if not ensure_project(headers):
        sys.exit(1)
    url = deploy(headers)
    if url:
        print(f"\nDashboard live at: {url}")
        prod_url = f"https://{PROJECT_NAME}.pages.dev"
        print(f"Production URL: {prod_url}")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
