"""LLM-yhteys. Kaikki presetit puhuvat OpenAI-yhteensopivaa rajapintaa, joten
sama agentti toimii ilmaisilla malleilla ja myöhemmin Azuressa vain ympäristömuuttujia vaihtamalla.

Ympäristömuuttujat:
  LLM_PROVIDER  gemini | github | ollama | azure   (oletus gemini)
  LLM_API_KEY   avain (tai presetin oma muuttuja, esim. GEMINI_API_KEY)
  LLM_MODEL     mallin nimi (ohittaa presetin oletuksen)
  LLM_BASE_URL  rajapinnan osoite (ohittaa presetin oletuksen; pakollinen azurelle)

Mallinimet vaihtuvat usein: tarkista ajantasainen nimi palveluntarjoajalta.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

PRESETS = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-3.5-flash-lite",  # ilmaistasolla väljemmät rajat; tarkempi: LLM_MODEL=gemini-3.8-flash
        "key_env": "GEMINI_API_KEY",
    },
    "github": {
        "base_url": "https://models.github.ai/inference",
        "model": "openai/gpt-4.1-mini",
        "key_env": "GITHUB_TOKEN",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b",
        "key_env": None,
    },
    "azure": {
        "base_url": None,  # esim. https://<resurssi>.openai.azure.com/openai/v1/
        "model": None,     # deploymentin nimi
        "key_env": "AZURE_OPENAI_API_KEY",
    },
}


@dataclass
class LLMSettings:
    provider: str
    base_url: str
    model: str
    api_key: str


def llm_settings(env: dict | None = None) -> LLMSettings:
    env = os.environ if env is None else env
    provider = env.get("LLM_PROVIDER", "gemini").strip().lower()
    if provider not in PRESETS:
        raise ValueError(f"LLM_PROVIDER pitää olla yksi näistä: {sorted(PRESETS)}")
    preset = PRESETS[provider]
    base_url = env.get("LLM_BASE_URL") or preset["base_url"]
    model = env.get("LLM_MODEL") or preset["model"]
    key = env.get("LLM_API_KEY") or (env.get(preset["key_env"]) if preset["key_env"] else None)
    if provider == "ollama":
        key = key or "ollama"
    if not base_url:
        raise ValueError(f"{provider}: aseta LLM_BASE_URL")
    if not model:
        raise ValueError(f"{provider}: aseta LLM_MODEL")
    if not key:
        hint = preset["key_env"] or "LLM_API_KEY"
        raise ValueError(f"{provider}: aseta API-avain muuttujaan {hint} tai LLM_API_KEY")
    return LLMSettings(provider, base_url, model, key)


def make_client(settings: LLMSettings | None = None):
    from openai import OpenAI

    s = settings or llm_settings()
    return OpenAI(base_url=s.base_url, api_key=s.api_key, max_retries=3, timeout=120), s.model
