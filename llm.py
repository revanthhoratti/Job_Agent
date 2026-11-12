"""LLM access. Default provider is a local Ollama server (free, no API key).

config.yaml:
  provider: ollama            # or "anthropic" (needs ANTHROPIC_API_KEY and pip install anthropic)
  model: llama3.1:8b
  ollama_host: http://localhost:11434
  ollama_num_ctx: 8192        # context window; Ollama's default (2048) would cut off job descriptions
"""
import json
import os
import re

import requests

SETTINGS = {
    "provider": "ollama",
    "ollama_host": "http://localhost:11434",
    "ollama_num_ctx": 8192,
}


def configure(cfg: dict):
    for k in SETTINGS:
        if k in cfg:
            SETTINGS[k] = cfg[k]


def _ollama(prompt, system, model, max_tokens, json_mode):
    body = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "options": {
            "num_ctx": int(SETTINGS["ollama_num_ctx"]),
            "num_predict": max_tokens,
            "temperature": 0.2,
        },
    }
    if json_mode:
        body["format"] = "json"
    try:
        r = requests.post(f"{SETTINGS['ollama_host']}/api/chat", json=body, timeout=600)
    except requests.ConnectionError:
        raise SystemExit(
            "Can't reach Ollama at "
            f"{SETTINGS['ollama_host']}. Start it (open the Ollama app, or run `ollama serve`)."
        )
    if r.status_code == 404:
        raise SystemExit(f"Ollama doesn't have the model '{model}'. Run: ollama pull {model}")
    r.raise_for_status()
    return r.json()["message"]["content"]


def _anthropic(prompt, system, model, max_tokens):
    import anthropic

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip().strip("\"'")
    if not key:
        raise SystemExit("Set ANTHROPIC_API_KEY, or use provider: ollama in config.yaml.")
    try:
        msg = anthropic.Anthropic(api_key=key).messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError:
        raise SystemExit("Anthropic rejected your API key (401). Use provider: ollama to run free.")
    return "".join(b.text for b in msg.content if b.type == "text")


def ask(prompt: str, system: str, model: str, max_tokens: int = 1500, json_mode: bool = False) -> str:
    if SETTINGS["provider"] == "anthropic":
        return _anthropic(prompt, system, model, max_tokens)
    return _ollama(prompt, system, model, max_tokens, json_mode)


def ask_json(prompt: str, system: str, model: str, max_tokens: int = 1000) -> dict:
    text = ask(prompt, system, model, max_tokens, json_mode=True)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"No JSON in model reply: {text[:200]}")
    return json.loads(m.group(0))

