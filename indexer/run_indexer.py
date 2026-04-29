"""
Phase 2 — One-shot script that creates the AI Search datasource, skillset,
index, and indexer using managed-identity auth.

Run AFTER `azd up` (or `az deployment group create`) has provisioned infra
and AFTER the 2 PDFs are uploaded to the kb-pdfs blob container.

    python indexer/run_indexer.py

TODO (next phase): implement using azure-search-documents SDK; this is a
stub so the project structure is complete.
"""

if __name__ == "__main__":
    raise NotImplementedError("Implement in Phase 2.")
