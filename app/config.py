"""Application settings, loaded from environment variables and an optional .env file."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader (KEY=VALUE per line, '#' comments). Existing env vars win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _get_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    ollama_host: str
    ollama_model: str
    base_url: str
    api_key: str
    model: str
    llm_timeout: int
    job_timeout: int
    job_ttl: int
    max_concurrent_jobs: int
    search_fallback_url: str
    port: int
    db_path: Path

    @property
    def model_name(self) -> str:
        return self.ollama_model if self.llm_provider == "ollama" else self.model


def load_settings() -> Settings:
    _load_dotenv(BASE_DIR / ".env")
    provider = os.environ.get("LLM_PROVIDER", "ollama").strip().lower()
    if provider not in {"ollama", "openai_compatible"}:
        provider = "ollama"
    db_raw = os.environ.get("DB_PATH", "data/pazhuhesh_jostejoo.db").strip()
    db_path = Path(db_raw)
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path
    legacy_db = BASE_DIR / "data" / "pazhuheshyar.db"  # keep history from older installs
    if not db_path.exists() and legacy_db.is_file():
        db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(legacy_db, db_path)
    return Settings(
        llm_provider=provider,
        ollama_host=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").strip(),
        ollama_model=os.environ.get("OLLAMA_MODEL", "qwen3:4b").strip(),
        base_url=os.environ.get("BASE_URL", "").strip().rstrip("/"),
        api_key=os.environ.get("API_KEY", "").strip(),
        model=os.environ.get("MODEL", "").strip(),
        llm_timeout=_get_int("LLM_TIMEOUT_SECONDS", 180),
        job_timeout=_get_int("JOB_TIMEOUT_SECONDS", 300, minimum=30),
        job_ttl=_get_int("JOB_TTL_SECONDS", 3600, minimum=60),
        max_concurrent_jobs=_get_int("MAX_CONCURRENT_JOBS", 3),
        search_fallback_url=os.environ.get("SEARCH_FALLBACK_URL", "").strip().rstrip("/"),
        port=_get_int("PORT", 8000),
        db_path=db_path,
    )
