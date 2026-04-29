"""Phase 3 - shared config + lazy singletons (AOAI client, credential)."""

from __future__ import annotations

import os
from functools import lru_cache

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI

# ---- env -------------------------------------------------------------------

AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_OPENAI_CHAT_DEPLOYMENT = os.environ.get(
    "AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4.1-mini"
)
AZURE_OPENAI_EMBEDDING_DEPLOYMENT = os.environ.get(
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large"
)
AZURE_OPENAI_EMBEDDING_DIM = int(os.environ.get("AZURE_OPENAI_EMBEDDING_DIM", "3072"))

AZURE_SEARCH_ENDPOINT = os.environ.get("AZURE_SEARCH_ENDPOINT", "")
AZURE_SEARCH_INDEX = os.environ.get("AZURE_SEARCH_INDEX", "kb-index")
AZURE_SEARCH_API_VERSION = os.environ.get("AZURE_SEARCH_API_VERSION", "2024-07-01")
AZURE_SEARCH_TOP_K = int(os.environ.get("AZURE_SEARCH_TOP_K", "5"))
AZURE_SEARCH_VECTOR_K = int(os.environ.get("AZURE_SEARCH_VECTOR_K", "50"))

REDIS_HOST = os.environ.get("REDIS_HOST", "")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6380"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")
CACHE_SIM_THRESHOLD = float(os.environ.get("CACHE_SIM_THRESHOLD", "0.97"))
CACHE_MAX_ENTRIES = int(os.environ.get("CACHE_MAX_ENTRIES", "500"))
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "3600"))

SYSTEM_PROMPT = (
    "You are a concise voice assistant for an insurance knowledge base. "
    "Answer ONLY from the provided sources. If the answer is not in the sources, "
    "say you don't have that information. Keep answers under 60 spoken words. "
    "Cite sources inline as [doc#] tags."
)

# ---- singletons ------------------------------------------------------------


@lru_cache(maxsize=1)
def credential() -> DefaultAzureCredential:
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


@lru_cache(maxsize=1)
def aoai_client() -> AsyncAzureOpenAI:
    if not AZURE_OPENAI_ENDPOINT:
        raise RuntimeError("AZURE_OPENAI_ENDPOINT is not set")
    token_provider = get_bearer_token_provider(
        credential(), "https://cognitiveservices.azure.com/.default"
    )
    return AsyncAzureOpenAI(
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        api_version=AZURE_OPENAI_API_VERSION,
        azure_ad_token_provider=token_provider,
    )
