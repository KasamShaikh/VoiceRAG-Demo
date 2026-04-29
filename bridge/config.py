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

# ---- Voice Live (Phase 4) --------------------------------------------------
#
# Wire protocol matches Azure AI Voice Live / AOAI Realtime. The relay opens a
# WS to AZURE_VOICE_LIVE_WSS_URL (model + api-version appended if not already).
#
# Default points at AOAI realtime via the same AOAI account; if the workspace
# uses the Speech-hosted Voice Live endpoint instead, override the env var.
AZURE_VOICE_LIVE_API_VERSION = os.environ.get(
    "AZURE_VOICE_LIVE_API_VERSION", "2025-04-01-preview"
)
AZURE_VOICE_LIVE_MODEL = os.environ.get(
    "AZURE_VOICE_LIVE_MODEL", "gpt-4o-mini-realtime-preview"
)
AZURE_VOICE_LIVE_VOICE = os.environ.get("AZURE_VOICE_LIVE_VOICE", "alloy")
AZURE_VOICE_LIVE_WSS_URL = os.environ.get("AZURE_VOICE_LIVE_WSS_URL", "")
AZURE_VOICE_LIVE_SCOPE = os.environ.get(
    "AZURE_VOICE_LIVE_SCOPE", "https://cognitiveservices.azure.com/.default"
)

# ---- Speech (Phase 5 composed path) ----------------------------------------
AZURE_SPEECH_REGION = os.environ.get("AZURE_SPEECH_REGION", "")
AZURE_SPEECH_ENDPOINT = os.environ.get("AZURE_SPEECH_ENDPOINT", "")
AZURE_SPEECH_STT_LANGUAGE = os.environ.get("AZURE_SPEECH_STT_LANGUAGE", "en-US")
AZURE_SPEECH_TTS_VOICE = os.environ.get("AZURE_SPEECH_TTS_VOICE", "en-US-JennyNeural")
AZURE_SPEECH_AUDIO_RATE = int(os.environ.get("AZURE_SPEECH_AUDIO_RATE", "24000"))


def voice_live_url() -> str:
    """Build the upstream WSS URL.

    Preference order:
      1. AZURE_VOICE_LIVE_WSS_URL (full URL, used as-is if it has query string).
      2. Derived from AZURE_OPENAI_ENDPOINT -> /openai/realtime.
    """
    base = AZURE_VOICE_LIVE_WSS_URL
    if not base:
        if not AZURE_OPENAI_ENDPOINT:
            raise RuntimeError("Set AZURE_VOICE_LIVE_WSS_URL or AZURE_OPENAI_ENDPOINT")
        host = AZURE_OPENAI_ENDPOINT.replace("https://", "").rstrip("/")
        base = f"wss://{host}/openai/realtime"
    if "?" in base:
        return base
    return (
        f"{base}?api-version={AZURE_VOICE_LIVE_API_VERSION}"
        f"&deployment={AZURE_VOICE_LIVE_MODEL}"
    )


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
