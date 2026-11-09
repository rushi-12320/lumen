"""Runs tests/cloud_flows.py in a separate process: Lumen Cloud is chosen by environment variables at import time."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def test_cloud_flows():
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", os.path.join(HERE, "cloud_flows.py")],
                       cwd=os.path.dirname(HERE), capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-6000:] + r.stderr[-3000:]
