"""Download Amazon RDS global-bundle.pem into the repo root.

Usage: python fetch_rds_cert.py
"""
from pathlib import Path
import urllib.request
import sys

URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"


def main():
    repo_root = Path(__file__).resolve().parents[2]
    dest = repo_root / "global-bundle.pem"
    try:
        print(f"Downloading RDS trust bundle to {dest} ...")
        with urllib.request.urlopen(URL, timeout=30) as r:
            data = r.read()
        dest.write_bytes(data)
        print("Downloaded and saved.")
        return 0
    except Exception as e:
        print("Failed to download cert:", e)
        return 1


if __name__ == '__main__':
    sys.exit(main())
