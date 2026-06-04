#!/usr/bin/env python3
"""
Deploy dashboard to Cloudflare Pages via Direct Upload API.

Uses the same 3-step upload flow as wrangler:
  1. Get upload JWT token
  2. Upload files to /pages/assets/upload
  3. Create deployment with manifest

Requires:
  - CLOUDFLARE_API_KEY + CLOUDFLARE_EMAIL (or CLOUDFLARE_API_TOKEN) in .env
  - blake3 package: pip install blake3

Usage:
  python3 scripts/deploy_cloudflare.py
"""

import base64
import json
import mimetypes
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    os.system(f"{sys.executable} -m pip install requests -q")
    import requests

try:
    import blake3
except ImportError:
    os.system(f"{sys.executable} -m pip install blake3 -q")
    import blake3

ACCOUNT_ID = "2b75c6fb3ad4b0b6c46408ffc96366a0"
PROJECT_NAME = "chanlun-dashboard"
BASE_URL = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/pages/projects"
ASSETS_API = "https://api.cloudflare.com/client/v4/pages/assets"

TIMEOUT_SHORT = 30   # seconds, for metadata API calls
TIMEOUT_UPLOAD = 180  # seconds, for file upload (large payload)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = PROJECT_ROOT / "reports"
ENV_FILE = PROJECT_ROOT / ".env"

# Only deploy these files (mobile-only mode for fast deploy)
DEPLOY_FILES = ["dashboard_mobile.html", "index.html"]

MAX_BUCKET_SIZE = 20 * 1024 * 1024  # 20 MiB per batch (base64 inflates ~33%)
MAX_BUCKET_FILES = 150


def load_credentials():
    """Load Cloudflare credentials from env or .env file."""
    env_vars = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip().strip('"').strip("'")

    api_token = os.environ.get("CLOUDFLARE_API_TOKEN", env_vars.get("CLOUDFLARE_API_TOKEN", ""))
    if api_token:
        return {"Authorization": "Bearer " + api_token}

    api_key = os.environ.get("CLOUDFLARE_API_KEY", env_vars.get("CLOUDFLARE_API_KEY", ""))
    email = os.environ.get("CLOUDFLARE_EMAIL", env_vars.get("CLOUDFLARE_EMAIL", ""))
    if api_key and email:
        return {"X-Auth-Key": api_key, "X-Auth-Email": email}

    print("ERROR: No Cloudflare credentials found.")
    print(f"Set in environment or {ENV_FILE}:")
    print("  Option A: CLOUDFLARE_API_TOKEN=your_token")
    print("  Option B: CLOUDFLARE_API_KEY=your_key + CLOUDFLARE_EMAIL=your_email")
    sys.exit(1)


def hash_file(filepath):
    """Compute file hash matching wrangler's format: blake3(base64(content) + ext)[:32]"""
    content = filepath.read_bytes()
    b64_content = base64.b64encode(content).decode("ascii")
    extension = filepath.suffix.lstrip(".")
    hash_input = b64_content + extension
    return blake3.blake3(hash_input.encode("ascii")).hexdigest()[:32]


def ensure_project(headers):
    """Create project if not exists."""
    r = requests.get(f"{BASE_URL}/{PROJECT_NAME}", headers=headers, timeout=TIMEOUT_SHORT)
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        print(f"Creating project '{PROJECT_NAME}'...")
        r = requests.post(BASE_URL, headers=headers, json={
            "name": PROJECT_NAME,
            "production_branch": "main",
        }, timeout=TIMEOUT_SHORT)
        if r.status_code in (200, 201):
            print(f"Project created: {PROJECT_NAME}")
            return True
        print(f"Failed to create project: {r.status_code} {r.text[:200]}")
        return False
    print(f"Error checking project: {r.status_code} {r.text[:200]}")
    return False


def collect_files(deploy_dir, delta_only=False):
    """Collect files for deployment.

    delta_only=False: full deploy (HTML + all data/*.js)
    delta_only=True:  delta deploy (HTML + data/live.js only)
    """
    files = []
    mobile_html = deploy_dir / "dashboard_mobile.html"
    if not mobile_html.exists():
        print(f"ERROR: {mobile_html} not found")
        return files

    h = hash_file(mobile_html)
    size = mobile_html.stat().st_size
    files.append({"path": "/index.html", "hash": h, "content_type": "text/html",
                  "abs_path": mobile_html, "size": size})

    data_dir = deploy_dir / "data"
    if not data_dir.is_dir():
        return files

    if delta_only:
        live_js = data_dir / "live.js"
        if live_js.exists():
            fh = hash_file(live_js)
            files.append({"path": "/data/live.js", "hash": fh,
                          "content_type": "application/javascript",
                          "abs_path": live_js, "size": live_js.stat().st_size})
        else:
            print("WARNING: data/live.js not found, delta deploy has no chart data")
    else:
        for p in sorted(data_dir.iterdir()):
            if p.is_file() and p.suffix == ".js":
                rel = "/data/" + p.name
                fh = hash_file(p)
                files.append({"path": rel, "hash": fh,
                              "content_type": "application/javascript",
                              "abs_path": p, "size": p.stat().st_size})
    return files


def get_upload_token(headers):
    """Get JWT token for uploading assets."""
    url = f"{BASE_URL}/{PROJECT_NAME}/upload-token"
    r = requests.get(url, headers=headers, timeout=TIMEOUT_SHORT)
    if r.status_code == 200:
        data = r.json()
        jwt = data.get("result", {}).get("jwt", "")
        if jwt:
            return jwt
    print(f"Failed to get upload token: {r.status_code} {r.text[:200]}")
    return None


def upload_files(jwt, files):
    """Upload files in batches to /pages/assets/upload."""
    all_hashes = [f["hash"] for f in files]

    # Check which files are missing (already uploaded ones can be skipped)
    check_headers = {"Authorization": "Bearer " + jwt, "Content-Type": "application/json"}
    r = requests.post(f"{ASSETS_API}/check-missing", headers=check_headers,
                      json={"hashes": all_hashes}, timeout=TIMEOUT_SHORT)
    if r.status_code != 200:
        print(f"Warning: check-missing failed ({r.status_code}), uploading all files")
        missing_hashes = set(all_hashes)
    else:
        missing_hashes = set(r.json() if isinstance(r.json(), list) else r.json().get("result", []))

    files_to_upload = [f for f in files if f["hash"] in missing_hashes]
    if not files_to_upload:
        print(f"  All {len(files)} files already cached, skipping upload.")
        return True

    print(f"  Uploading {len(files_to_upload)} files ({len(files) - len(files_to_upload)} cached)...")

    # Split into buckets respecting size limits
    buckets = []
    current_bucket = []
    current_size = 0
    for f in sorted(files_to_upload, key=lambda x: x["size"], reverse=True):
        if current_size + f["size"] > MAX_BUCKET_SIZE or len(current_bucket) >= MAX_BUCKET_FILES:
            if current_bucket:
                buckets.append(current_bucket)
            current_bucket = [f]
            current_size = f["size"]
        else:
            current_bucket.append(f)
            current_size += f["size"]
    if current_bucket:
        buckets.append(current_bucket)

    upload_headers = {"Authorization": "Bearer " + jwt, "Content-Type": "application/json"}

    for i, bucket in enumerate(buckets):
        payload = []
        for f in bucket:
            content = f["abs_path"].read_bytes()
            payload.append({
                "key": f["hash"],
                "value": base64.b64encode(content).decode("ascii"),
                "metadata": {"contentType": f["content_type"]},
                "base64": True,
            })

        for attempt in range(3):
            r = requests.post(f"{ASSETS_API}/upload", headers=upload_headers,
                              json=payload, timeout=TIMEOUT_UPLOAD)
            if r.status_code in (200, 201):
                break
            if attempt < 2:
                time.sleep(2 ** attempt)
        else:
            print(f"  Bucket {i+1}/{len(buckets)} FAILED: {r.status_code} {r.text[:200]}")
            return False

        print(f"  Bucket {i+1}/{len(buckets)} uploaded ({len(bucket)} files)")

    return True


def create_deployment(headers, manifest):
    """Create deployment with the file manifest."""
    deploy_url = f"{BASE_URL}/{PROJECT_NAME}/deployments"
    multipart = [("manifest", (None, json.dumps(manifest), "application/json"))]
    r = requests.post(deploy_url, headers=headers, files=multipart, timeout=TIMEOUT_SHORT)
    if r.status_code in (200, 201):
        data = r.json()
        result = data.get("result", {})
        return result.get("url", ""), result.get("id", "")
    print(f"Deployment creation failed: {r.status_code}")
    try:
        for e in r.json().get("errors", []):
            print(f"  {e.get('code')}: {e.get('message')}")
    except Exception:
        print(f"  {r.text[:300]}")
    return None, None


MANIFEST_CACHE = DEPLOY_DIR / "data" / ".last_manifest.json"


def deploy(delta_only=False, save_baseline=False):
    """Deployment pipeline.

    delta_only: deploy only HTML + live.js (reuse full manifest for other files)
    save_baseline: after full deploy, save baseline for future deltas
    """
    headers = load_credentials()

    if not ensure_project(headers):
        sys.exit(1)

    if delta_only:
        files_to_upload, manifest = _prepare_delta_deploy(DEPLOY_DIR)
    else:
        files_to_upload = collect_files(DEPLOY_DIR, delta_only=False)
        manifest = {f["path"]: f["hash"] for f in files_to_upload}

    if not files_to_upload:
        print("No files to deploy!")
        sys.exit(1)

    mode = "DELTA" if delta_only else "FULL"
    total_size = sum(f["size"] for f in files_to_upload)
    print(f"[{mode}] Deploying: upload {len(files_to_upload)} files ({total_size / 1024:.0f} KB), "
          f"manifest {len(manifest)} paths")

    t0 = time.time()

    jwt = get_upload_token(headers)
    if not jwt:
        sys.exit(1)

    if not upload_files(jwt, files_to_upload):
        sys.exit(1)

    url, deploy_id = create_deployment(headers, manifest)
    elapsed = time.time() - t0

    if url:
        print(f"\nDeploy OK in {elapsed:.1f}s")
        print(f"  ID: {deploy_id}")
        print(f"  URL: {url}")
        prod_url = f"https://{PROJECT_NAME}-9d9.pages.dev"
        print(f"  Production: {prod_url}")
    else:
        sys.exit(1)

    if not delta_only:
        # Save manifest for future delta deploys
        with open(MANIFEST_CACHE, "w") as f:
            json.dump(manifest, f)
        if save_baseline:
            _save_baseline_from_data_dir(DEPLOY_DIR / "data")


def _prepare_delta_deploy(deploy_dir):
    """Prepare delta deploy: upload only HTML + live.js, merge with cached manifest."""
    if not MANIFEST_CACHE.exists():
        print("No cached manifest found, falling back to full deploy")
        files = collect_files(deploy_dir, delta_only=False)
        return files, {f["path"]: f["hash"] for f in files}

    with open(MANIFEST_CACHE, "r") as f:
        old_manifest = json.load(f)

    files_to_upload = []
    new_manifest = dict(old_manifest)

    # HTML
    mobile_html = deploy_dir / "dashboard_mobile.html"
    if mobile_html.exists():
        h = hash_file(mobile_html)
        files_to_upload.append({"path": "/index.html", "hash": h,
                                "content_type": "text/html",
                                "abs_path": mobile_html, "size": mobile_html.stat().st_size})
        new_manifest["/index.html"] = h

    # live.js
    live_js = deploy_dir / "data" / "live.js"
    if live_js.exists():
        h = hash_file(live_js)
        files_to_upload.append({"path": "/data/live.js", "hash": h,
                                "content_type": "application/javascript",
                                "abs_path": live_js, "size": live_js.stat().st_size})
        new_manifest["/data/live.js"] = h

    return files_to_upload, new_manifest


def _save_baseline_from_data_dir(data_dir: Path):
    """Read all data/*.js files and save bar counts as baseline."""
    import re
    baseline = {"time": time.strftime("%Y-%m-%d %H:%M"), "bars": {}}
    pattern = re.compile(r'"dates":\[([^\]]*)\]')
    for p in sorted(data_dir.iterdir()):
        if not p.is_file() or p.suffix != ".js" or p.name == "live.js":
            continue
        key = p.stem
        content = p.read_text(encoding="utf-8")
        m = pattern.search(content)
        if m:
            dates_str = m.group(1)
            n_bars = dates_str.count('"') // 2 if dates_str else 0
            baseline["bars"][key] = n_bars

    out = data_dir / ".baseline.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(baseline, f, ensure_ascii=False)
    print(f"  Baseline saved: {len(baseline['bars'])} keys")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Deploy to Cloudflare Pages")
    parser.add_argument("--delta", action="store_true",
                        help="Delta deploy: only HTML + live.js")
    parser.add_argument("--save-baseline", action="store_true",
                        help="After full deploy, save baseline for future deltas")
    args = parser.parse_args()
    deploy(delta_only=args.delta, save_baseline=args.save_baseline)
