"""
Phase 2 - Create-or-update AI Search datasource, index, skillset, indexer
for the kb-pdfs corpus, then run the indexer and wait for completion.

Auth: DefaultAzureCredential. Required RBAC is set by infra/main.bicep.

Required env (load via `azd env get-values > .env` or set manually):
  AZURE_SEARCH_ENDPOINT
  AZURE_SEARCH_INDEX                 default: kb-index
  AZURE_OPENAI_ENDPOINT
  AZURE_OPENAI_EMBEDDING_DEPLOYMENT
  AZURE_STORAGE_ACCOUNT
  AZURE_STORAGE_CONTAINER            default: kb-pdfs
  AZURE_SUBSCRIPTION_ID
  AZURE_RESOURCE_GROUP

Usage:
  python indexer/run_indexer.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx
from azure.identity import DefaultAzureCredential

API = "2024-11-01-preview"
ROOT = Path(__file__).resolve().parent
INDEX_FILE = ROOT / "index.json"
SKILLSET_FILE = ROOT / "skillset.json"

DATASOURCE_NAME = "kb-datasource"
SKILLSET_NAME = "kb-skillset"
INDEXER_NAME = "kb-indexer"


def env(name, default=None):
    v = os.environ.get(name, default)
    if not v:
        sys.exit(f"ERROR: env var {name} is not set.")
    return v


def expand(value):
    if isinstance(value, str):
        out = value
        for k, v in os.environ.items():
            out = out.replace("${" + k + "}", v)
        return out
    if isinstance(value, list):
        return [expand(x) for x in value]
    if isinstance(value, dict):
        return {k: expand(v) for k, v in value.items()}
    return value


def load_json_with_env(path):
    return expand(json.loads(path.read_text(encoding="utf-8")))


class SearchAdmin:
    def __init__(self, endpoint, token):
        self.endpoint = endpoint.rstrip("/")
        self.client = httpx.Client(
            timeout=120.0,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )

    def put(self, path, body):
        url = f"{self.endpoint}{path}?api-version={API}"
        r = self.client.put(url, json=body)
        if r.status_code not in (200, 201, 204):
            sys.exit(f"PUT {path} failed [{r.status_code}]: {r.text}")
        print(f"  [ok] PUT {path}")

    def post(self, path, body=None):
        url = f"{self.endpoint}{path}?api-version={API}"
        r = self.client.post(url, json=body or {})
        if r.status_code not in (200, 201, 202, 204):
            sys.exit(f"POST {path} failed [{r.status_code}]: {r.text}")
        return r.json() if r.text else {}

    def get(self, path):
        url = f"{self.endpoint}{path}?api-version={API}"
        r = self.client.get(url)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return r.text


def build_datasource():
    sub = env("AZURE_SUBSCRIPTION_ID")
    rg = env("AZURE_RESOURCE_GROUP")
    account = env("AZURE_STORAGE_ACCOUNT")
    container = os.environ.get("AZURE_STORAGE_CONTAINER", "kb-pdfs")
    return {
        "name": DATASOURCE_NAME,
        "type": "azureblob",
        "credentials": {
            "connectionString": (
                f"ResourceId=/subscriptions/{sub}/resourceGroups/{rg}"
                f"/providers/Microsoft.Storage/storageAccounts/{account};"
            ),
        },
        "container": {"name": container},
        "dataChangeDetectionPolicy": {
            "@odata.type": "#Microsoft.Azure.Search.HighWaterMarkChangeDetectionPolicy",
            "highWaterMarkColumnName": "metadata_storage_last_modified",
        },
    }


def build_indexer():
    return {
        "name": INDEXER_NAME,
        "dataSourceName": DATASOURCE_NAME,
        "targetIndexName": env("AZURE_SEARCH_INDEX", "kb-index"),
        "skillsetName": SKILLSET_NAME,
        "parameters": {
            "batchSize": 1,
            "maxFailedItems": 0,
            "configuration": {
                "dataToExtract": "contentAndMetadata",
                "parsingMode": "default",
                "allowSkillsetToReadFileData": True,
            },
        },
        "fieldMappings": [
            {
                "sourceFieldName": "metadata_storage_path",
                "targetFieldName": "id",
                "mappingFunction": {"name": "base64Encode"},
            }
        ],
    }


def main():
    endpoint = env("AZURE_SEARCH_ENDPOINT")
    index_name = env("AZURE_SEARCH_INDEX", "kb-index")

    print(f"Search endpoint: {endpoint}")
    print(f"Target index:    {index_name}\n")

    cred = DefaultAzureCredential(exclude_interactive_browser_credential=False)
    token = cred.get_token("https://search.azure.com/.default").token
    admin = SearchAdmin(endpoint, token)

    print("[1/5] Datasource ...")
    admin.put(f"/datasources('{DATASOURCE_NAME}')", build_datasource())

    print("[2/5] Index ...")
    index_body = load_json_with_env(INDEX_FILE)
    index_body["name"] = index_name
    admin.put(f"/indexes('{index_name}')", index_body)

    print("[3/5] Skillset ...")
    skillset_body = load_json_with_env(SKILLSET_FILE)
    skillset_body["name"] = SKILLSET_NAME
    admin.put(f"/skillsets('{SKILLSET_NAME}')", skillset_body)

    print("[4/5] Indexer ...")
    admin.put(f"/indexers('{INDEXER_NAME}')", build_indexer())

    print("[5/5] Run indexer ...")
    admin.post(f"/indexers('{INDEXER_NAME}')/search.run")

    print("\nPolling indexer status (every 5s, up to 5 min) ...")
    deadline = time.time() + 300
    last_status = ""
    while time.time() < deadline:
        st = admin.get(f"/indexers('{INDEXER_NAME}')/search.status")
        last = (st.get("lastResult") if isinstance(st, dict) else None) or {}
        status = (
            last.get("status")
            or (st.get("status") if isinstance(st, dict) else None)
            or "unknown"
        )
        if status != last_status:
            print(f"  status={status}")
            last_status = status
        if status in ("success", "transientFailure", "persistentFailure", "reset"):
            break
        time.sleep(5)

    final = admin.get(f"/indexers('{INDEXER_NAME}')/search.status")
    last = (final.get("lastResult") if isinstance(final, dict) else None) or {}
    print("\n=== Indexer last result ===")
    print(
        json.dumps(
            {
                "status": last.get("status"),
                "itemsProcessed": last.get("itemsProcessed"),
                "itemsFailed": last.get("itemsFailed"),
                "errors": last.get("errors"),
                "warnings": (last.get("warnings") or [])[:3],
                "startTime": last.get("startTime"),
                "endTime": last.get("endTime"),
            },
            indent=2,
            default=str,
        )
    )

    if last.get("status") != "success":
        return 1

    count = admin.get(f"/indexes('{index_name}')/docs/$count")
    print(f"\nIndex now contains {count} documents.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
