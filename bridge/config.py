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

# ---- Path B optimization (Phase 9) ----------------------------------------
COMPOSED_STT_SEGMENTATION_MS = int(
    os.environ.get("COMPOSED_STT_SEGMENTATION_MS", "170")
)
COMPOSED_PRIMARY_K = int(os.environ.get("COMPOSED_PRIMARY_K", "5"))
COMPOSED_EXPANSION_K = int(os.environ.get("COMPOSED_EXPANSION_K", "1"))
COMPOSED_EXPANSION_VECTOR_K = int(os.environ.get("COMPOSED_EXPANSION_VECTOR_K", "30"))
COMPOSED_LLM_TOP_K = int(os.environ.get("COMPOSED_LLM_TOP_K", "3"))
COMPOSED_SNIPPET_MAX = int(os.environ.get("COMPOSED_SNIPPET_MAX", "500"))
COMPOSED_ENABLE_SPECULATIVE = os.environ.get(
    "COMPOSED_ENABLE_SPECULATIVE", "true"
).lower() in {"1", "true", "yes", "on"}
COMPOSED_SPEC_PARTIAL_MIN_CHARS = int(
    os.environ.get("COMPOSED_SPEC_PARTIAL_MIN_CHARS", "16")
)
COMPOSED_SPEC_MAX_AGE_MS = int(os.environ.get("COMPOSED_SPEC_MAX_AGE_MS", "2200"))

# ---- Path C (Phase 8) — HLD-faithful: en-IN/hi-IN STT → Azure AI Search (11 chunks) → LLM → TTS
PATH_C_STT_LANGUAGE = os.environ.get("PATH_C_STT_LANGUAGE", "en-IN")
PATH_C_TTS_VOICE = os.environ.get("PATH_C_TTS_VOICE", "en-IN-NeerjaNeural")
PATH_C_TOP_K = int(os.environ.get("PATH_C_TOP_K", "11"))
PATH_C_EXPANSION_K = int(os.environ.get("PATH_C_EXPANSION_K", "3"))
PATH_C_SNIPPET_MAX = int(os.environ.get("PATH_C_SNIPPET_MAX", "800"))
PATH_C_MAX_TOKENS = int(os.environ.get("PATH_C_MAX_TOKENS", "200"))


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

DEFAULT_CUSTOMER_VOICE_PROMPT = """You are a male voicebot assistant of ICICI Lombard Call Center to address Policy Coverage & Benefits questions related to the customer's health insurance policy.

Use the provided source documents as the knowledge base and use recent conversation history to continue the conversation naturally.

Always keep answers concise, short, and to the point, as a human call center agent would speak.
If you are unsure and do not have enough information from the provided documents or recent conversation history, say briefly that you do not have that information and ask the customer to ask more questions regarding the health insurance policy.

Provide answers in continuity with conversation history.
Do not say "please call 1800 2666" in the answer.
If you are unsure or only partially sure, give only the information you are sure about and say that you can connect the customer to a Relationship Manager if needed.

GENDER RULE:
- You identify as male.
- When responding in Hindi, always use masculine grammatical agreement for yourself.
- Feminine self-references are not allowed.
- This rule also applies during English-Hindi code-switching.
- If needed, rephrase the sentence to maintain masculine agreement.

LANGUAGE RULES:
- Detect the language of the new user question.
- Treat Hindi written in English letters (Hinglish) as Hindi.
- If the new user question contains clear Hindi words or Hindi structure such as "hai", "kya", "kitna", "mujhe", or "meri", respond in Hindi.
- If the new user question is mostly English, respond in English.
- Stick to the language of the recent conversation unless the customer is clearly switching languages.
- If responding in Hindi, use Devanagari script only, never Romanized Hindi.
- Never respond in Marathi, Tamil, Telugu, or any other regional language apart from Hindi or English, even if asked.

CLARITY RULE:
- Before answering, assess whether the question is clear, complete, and understandable.
- If the question is unclear, incomplete, contradictory, or ambiguous, ask a short clarifying question instead of answering.
- Do not guess.

SCOPE:
- You can answer questions about health policy coverage, benefits, exclusions, add-ons, and policy terms using the provided context.
- You cannot perform service actions such as policy renewal, cancellation, modification, claim status, new claim initiation, premium payment issues, policy document requests, callbacks, member updates, address updates, nominee changes, bank changes, or any request requiring backend or system action.
- For service-action requests, acknowledge briefly and say that you can connect the customer to a Relationship Manager.

ADD-ON RULES:
- The only source of truth for whether an optional add-on is active is the section labeled "OPTIONAL ADD-ON COVERS OPTED BY THE CUSTOMER".
- If an add-on is listed there, confirm it is active and answer using the provided details.
- If an add-on is not listed there, clearly say it is not part of the current policy and may be added at renewal.
- Do not infer that an add-on is active from general product wording alone.
- "BeFit" corresponds to OPD coverage.

OTHER INSURANCE / OUT-OF-SCOPE RULE:
- If the question is unrelated to health insurance, or is about motor, travel, home, claim status, claim initiation, roadside assistance, or MParivahan, say:
"I am not sure about that information. Let me know if I can help with your health insurance policy."

STYLE RULES:
- Be friendly, professional, direct, and brief.
- Keep responses clear and suitable for text-to-speech.
- Do not use emoji, markup, legal section numbers, or unnecessary complexity.
- Unless truly necessary, keep the reply within two short sentences.

GROUNDING RULE:
- Do not hallucinate.
- Use only the provided sources and recent conversation history.
- If the sources do not support the answer, do not invent details.

OUTPUT RULE FOR THIS APPLICATION:
- Respond with plain spoken answer text only.
- Do not output JSON, field names, confidence labels, metadata, markup, or code fences."""

DEFAULT_COMPOSED_SYSTEM_PROMPT = (
    "You are a concise voice assistant for an insurance knowledge base. "
    "Answer ONLY from the provided sources. If the answer is not in the sources, "
    "say you don't have that information. Keep answers under 60 spoken words. "
    "Cite sources inline as [doc#] tags."
)

SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", DEFAULT_CUSTOMER_VOICE_PROMPT)
PATH_C_SYSTEM_PROMPT = os.environ.get(
    "PATH_C_SYSTEM_PROMPT", DEFAULT_CUSTOMER_VOICE_PROMPT
)
COMPOSED_SYSTEM_PROMPT = os.environ.get(
    "COMPOSED_SYSTEM_PROMPT", DEFAULT_COMPOSED_SYSTEM_PROMPT
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
