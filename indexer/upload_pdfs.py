"""
Phase 2 — Upload local PDFs to the kb-pdfs blob container.

Auth: DefaultAzureCredential (uses your `az login` / `azd auth login` identity).
The infra grants your principal `Storage Blob Data Contributor` if you set
AZURE_PRINCIPAL_ID before deploying.

Usage:
    python indexer/upload_pdfs.py              # uploads everything in ./data/*.pdf
    python indexer/upload_pdfs.py path1 path2  # uploads specific files
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


def env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(
            f"ERROR: env var {name} is not set. Run `azd env get-values >> .env` first."
        )
    return v


def main(argv: list[str]) -> int:
    account = env("AZURE_STORAGE_ACCOUNT")
    container = os.environ.get("AZURE_STORAGE_CONTAINER", "kb-pdfs")

    if argv:
        pdfs = [Path(p) for p in argv]
    else:
        pdfs = sorted(DATA_DIR.glob("*.pdf"))

    if not pdfs:
        sys.exit(
            f"ERROR: no PDFs found. Drop files in {DATA_DIR} or pass paths as args."
        )

    cred = DefaultAzureCredential(exclude_interactive_browser_credential=False)
    bsc = BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=cred)
    container_client = bsc.get_container_client(container)

    # Container is created by Bicep; this is a defensive no-op if it already exists.
    try:
        container_client.create_container()
    except Exception:  # noqa: BLE001
        pass

    for pdf in pdfs:
        if not pdf.is_file():
            print(f"[skip] {pdf} (not a file)")
            continue
        blob = container_client.get_blob_client(pdf.name)
        with pdf.open("rb") as f:
            blob.upload_blob(
                f,
                overwrite=True,
                content_settings=ContentSettings(content_type="application/pdf"),
            )
        size_kb = pdf.stat().st_size / 1024
        print(f"[ok]   {pdf.name:40s}  {size_kb:8.1f} KB")

    print(f"\nUploaded {len(pdfs)} PDF(s) to {account}/{container}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
