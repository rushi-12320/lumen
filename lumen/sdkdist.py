"""The Python SDK as a pip-installable wheel, built in memory from sdk/lumen_sdk.py (no build tools needed).

    pip install https://<your-lumen>/sdk/lumen_llm_sdk-<version>-py3-none-any.whl

The distribution is called lumen-llm-sdk because "lumen-sdk" on PyPI is someone else's project; the module you
import is still lumen_sdk. The wheel's URL changes with the SDK version, so the console and docs always show the
current one; GET /sdk/ lists it, and that page also works with `pip install --find-links`.
"""
import base64
import hashlib
import io
import os
import re
import threading
import zipfile

ROOT = os.path.join(os.path.dirname(__file__), "..")
SDK = os.path.join(ROOT, "sdk", "lumen_sdk.py")
DIST = "lumen-llm-sdk"
_cache, _lock = {}, threading.Lock()


def source():
    with open(SDK, "rb") as f:
        return f.read()


def version(src=None):
    m = re.search(rb'^__version__ = "([^"]+)"', src if src is not None else source(), re.M)
    return m.group(1).decode() if m else "0.0.0"


def _b64(digest):
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _build(src):
    ver, stem = version(src), DIST.replace("-", "_")
    info = f"{stem}-{ver}.dist-info"
    meta = (f"Metadata-Version: 2.1\nName: {DIST}\nVersion: {ver}\n"
            "Summary: Lumen SDK: traces and checks every OpenAI, Anthropic and Gemini call of your app\n"
            "Requires-Python: >=3.8\n\n"
            "import lumen_sdk; lumen_sdk.init(api_key=\"lmn_...\", host=\"https://your-lumen\")\n")
    files = {"lumen_sdk.py": src, f"{info}/METADATA": meta.encode(),
             f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: lumen\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
             f"{info}/top_level.txt": b"lumen_sdk\n"}
    record = [f"{name},sha256={_b64(hashlib.sha256(data).digest())},{len(data)}" for name, data in files.items()]
    files[f"{info}/RECORD"] = ("\n".join(record + [f"{info}/RECORD,,"]) + "\n").encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            zi = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))      # fixed time: the same file gives the same wheel
            zi.external_attr = 0o644 << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data)
    data = buf.getvalue()
    return f"{stem}-{ver}-py3-none-any.whl", data, hashlib.sha256(data).hexdigest()


def wheel():
    """-> (file name, bytes, sha256 hex) of the wheel for the SDK file as it is now."""
    src = source()
    key = hashlib.sha256(src).hexdigest()
    with _lock:
        if _cache.get("key") != key:
            _cache.update(key=key, value=_build(src))
        return _cache["value"]


def info():
    name, _, sha = wheel()
    return {"version": version(), "dist": DIST, "wheel": f"/sdk/{name}", "sha256": sha, "file": "/sdk/lumen_sdk.py"}
