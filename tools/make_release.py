"""Build dist/lumen.zip: the release the AWS app server deploys (upload it to s3://<bucket>/release/lumen.zip).

    python tools/make_release.py

Contains the code, website and deploy files - never data, secrets, caches or tests."""
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INCLUDE = ["lumen", "static", "site", "sdk", "tools", "deploy", "requirements.txt", "README.md", ".dockerignore"]
SKIP_DIRS = {"__pycache__", ".pytest_cache", "data", "dist", "node_modules", ".git"}
SKIP_EXT = (".pyc", ".db", ".db-wal", ".db-shm", ".env", ".key")


def main():
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    out = os.path.join(ROOT, "dist", "lumen.zip")
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in INCLUDE:
            p = os.path.join(ROOT, item)
            if os.path.isfile(p):
                z.write(p, item)
                n += 1
                continue
            for d, dirs, files in os.walk(p):
                dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
                for f in files:
                    if f.endswith(SKIP_EXT) or f == ".env":
                        continue
                    full = os.path.join(d, f)
                    z.write(full, os.path.relpath(full, ROOT))
                    n += 1
    print(f"{out}: {n} files, {os.path.getsize(out) / 1024:.0f} KB")


if __name__ == "__main__":
    main()
