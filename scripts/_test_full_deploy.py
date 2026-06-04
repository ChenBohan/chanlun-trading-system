#!/usr/bin/env python3
"""Quick test: run full deploy with progress logging."""
import sys, time
sys.path.insert(0, 'scripts')
from deploy_cloudflare import deploy
deploy(delta_only=False, save_baseline=True)
