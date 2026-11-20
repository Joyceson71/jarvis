"""Central settings for Jarvis. Everything can be overridden from .env."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent

PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "key_env": "GROQ_API_KEY",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
        "key_env": "GEMINI_API_KEY",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "model": "llama3.1",
        "key_env": "",
    },
}


def _flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass
class Settings:
    provider: str
    base_url: str
    model: str
    api_key: str
    user_name: str
    wake_words: list
    voice_rate: int
    api_enabled: bool
    api_host: str
    api_port: int
    api_token: str
    lock_port: int
    data_dir: Path

    @property
    def needs_key(self) -> bool:
        return bool(PROVIDERS.get(self.provider, {}).get("key_env"))

    @classmethod
    def load(cls) -> "Settings":
        if load_dotenv:
            load_dotenv(ROOT / ".env")

        provider = (os.getenv("JARVIS_PROVIDER") or "groq").strip().lower()
        if provider not in PROVIDERS:
            raise SystemExit(
                f"Unknown JARVIS_PROVIDER '{provider}'. Choose one of: {', '.join(PROVIDERS)}"
            )
        spec = PROVIDERS[provider]
        data_dir = Path(os.getenv("JARVIS_DATA_DIR") or ROOT / "data")
        data_dir.mkdir(parents=True, exist_ok=True)

        wake = [w.strip().lower() for w in (os.getenv("JARVIS_WAKE_WORD") or "jarvis").split(",")]
        api_enabled = _flag("JARVIS_API_ENABLED", True)

        return cls(
            provider=provider,
            base_url=(os.getenv("JARVIS_BASE_URL") or spec["base_url"]).rstrip("/"),
            model=os.getenv("JARVIS_MODEL") or spec["model"],
            api_key=os.getenv(spec["key_env"], "").strip() if spec["key_env"] else "",
            user_name=os.getenv("JARVIS_USER_NAME") or "Boss",
            wake_words=[w for w in wake if w] or ["jarvis"],
            voice_rate=_int("JARVIS_VOICE_RATE", 185),
            api_enabled=api_enabled,
            api_host=os.getenv("JARVIS_API_HOST") or "0.0.0.0",
            api_port=_int("JARVIS_API_PORT", 8765),
            api_token=_load_token(data_dir) if api_enabled else "",
            lock_port=_int("JARVIS_LOCK_PORT", 47831),
            data_dir=data_dir,
        )


def _load_token(data_dir: Path) -> str:
    env_token = (os.getenv("JARVIS_API_TOKEN") or "").strip()
    if env_token:
        return env_token
    path = data_dir / "api_token.txt"
    if path.exists():
        saved = path.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    token = secrets.token_urlsafe(24)
    path.write_text(token, encoding="utf-8")
    return token
