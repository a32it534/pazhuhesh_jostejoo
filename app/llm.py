"""Shared LLM interface with two interchangeable providers (Ollama / OpenAI-compatible)."""
from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

import httpx

from .config import Settings

Messages = list[dict[str, str]]


class LLMError(Exception):
    """Generic LLM failure. The message is user-facing (Persian)."""


class LLMUnavailable(LLMError):
    """The model server cannot be reached at all."""


# ---------------------------------------------------------------- JSON helpers

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def strip_think(text: str) -> str:
    """Remove qwen3-style <think> blocks (including unterminated ones)."""
    if "</think>" in text.lower() and "<think>" not in text.lower():
        text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]
    text = _THINK_RE.sub("", text)
    idx = text.lower().find("<think>")
    if idx != -1:
        text = text[:idx]
    return text.strip()


def safe_json_parse(text: str) -> Any | None:
    """Parse JSON from messy model output. Returns a dict/list, or None on failure.

    Handles ```json fences, <think> blocks and extra prose around the JSON.
    """
    if not isinstance(text, str):
        return None
    cleaned = strip_think(text)
    if not cleaned:
        return None
    candidates = [cleaned]
    match = _FENCE_RE.search(cleaned)
    if match:
        candidates.insert(0, match.group(1).strip())

    decoder = json.JSONDecoder()
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, (dict, list)):
                return obj
        except (ValueError, TypeError):
            pass
        # Prefer objects over arrays so stray "[1]" in prose is not mistaken for the payload.
        for opener in ("{", "["):
            start = cand.find(opener)
            while start != -1:
                try:
                    obj, _ = decoder.raw_decode(cand[start:])
                    if isinstance(obj, (dict, list)):
                        return obj
                except ValueError:
                    pass
                start = cand.find(opener, start + 1)
    return None


# ---------------------------------------------------------------- base class

class BaseLLM(ABC):
    name: str = "base"
    model: str = ""

    @abstractmethod
    async def complete(self, messages: Messages, json_mode: bool = False,
                       temperature: float = 0.2) -> str: ...

    @abstractmethod
    def stream(self, messages: Messages, temperature: float = 0.2) -> AsyncIterator[str]: ...

    @abstractmethod
    async def ping(self) -> tuple[bool, str]:
        """Return (available, Persian detail message)."""


# ---------------------------------------------------------------- Ollama

class OllamaLLM(BaseLLM):
    name = "ollama"

    def __init__(self, host: str, model: str, timeout: int) -> None:
        self.host = host
        self.model = model
        self.timeout = timeout
        try:
            import ollama  # lazy import: not needed for the other provider
        except ImportError as exc:  # pragma: no cover
            raise LLMError("بسته‌ی ollama نصب نیست. دستور pip install -r requirements.txt را اجرا کنید.") from exc
        self._ollama = ollama
        self._client = ollama.Client(host=host, timeout=timeout)

    def _prepare(self, messages: Messages) -> Messages:
        msgs = [dict(m) for m in messages]
        if "qwen3" in self.model.lower():  # disable slow "thinking" mode
            for m in reversed(msgs):
                if m.get("role") == "user":
                    m["content"] = m["content"] + "\n/no_think"
                    break
        return msgs

    def _map_error(self, exc: Exception) -> LLMError:
        if isinstance(exc, self._ollama.ResponseError):
            if getattr(exc, "status_code", None) == 404:
                return LLMError(f"مدل «{self.model}» روی Ollama پیدا نشد. دستور ollama pull {self.model} را اجرا کنید.")
            return LLMError(f"Ollama خطا داد: {exc}")
        if isinstance(exc, (httpx.ConnectError, ConnectionError, httpx.ConnectTimeout)):
            return LLMUnavailable("اتصال به Ollama برقرار نشد. مطمئن شوید Ollama روشن است (ollama serve).")
        if isinstance(exc, httpx.TimeoutException):
            return LLMError("مدل دیر پاسخ داد (زمان انتظار تمام شد). مدل کوچک‌تری انتخاب کنید یا سقف زمانی را بیشتر کنید.")
        return LLMError(f"خطای ناشناخته از مدل: {type(exc).__name__}")

    def _chat_sync(self, messages: Messages, json_mode: bool, temperature: float) -> str:
        try:
            resp = self._client.chat(
                model=self.model,
                messages=self._prepare(messages),
                format="json" if json_mode else None,
                options={"temperature": temperature},
            )
            return resp["message"]["content"] or ""
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc

    async def complete(self, messages: Messages, json_mode: bool = False,
                       temperature: float = 0.2) -> str:
        # The ollama client is blocking: run it in a worker thread so the event loop stays free.
        return await asyncio.to_thread(self._chat_sync, messages, json_mode, temperature)

    def _open_stream(self, messages: Messages, temperature: float):
        try:
            return iter(self._client.chat(
                model=self.model, messages=self._prepare(messages), stream=True,
                options={"temperature": temperature},
            ))
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc

    async def stream(self, messages: Messages, temperature: float = 0.2) -> AsyncIterator[str]:
        iterator = await asyncio.to_thread(self._open_stream, messages, temperature)
        sentinel = object()

        def _next():
            try:
                return next(iterator, sentinel)
            except Exception as exc:  # noqa: BLE001
                raise self._map_error(exc) from exc

        while True:
            chunk = await asyncio.to_thread(_next)
            if chunk is sentinel:
                break
            piece = chunk["message"]["content"]
            if piece:
                yield piece

    async def ping(self) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self.host.rstrip('/')}/api/tags")
                resp.raise_for_status()
                names = [m.get("name", "") for m in resp.json().get("models", [])]
        except Exception:  # noqa: BLE001
            return False, "Ollama در دسترس نیست. آن را اجرا کنید (ollama serve)."
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        if wanted not in names and self.model not in names:
            return False, f"Ollama روشن است ولی مدل «{self.model}» دریافت نشده. اجرا کنید: ollama pull {self.model}"
        return True, "مدل آماده است."


# ---------------------------------------------------------------- OpenAI-compatible

class OpenAICompatibleLLM(BaseLLM):
    name = "openai_compatible"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _check_config(self) -> None:
        if not self.base_url or not self.model:
            raise LLMUnavailable("تنظیمات BASE_URL و MODEL در فایل .env کامل نیست.")

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            raise LLMError("کلید API نامعتبر است یا دسترسی ندارد. API_KEY را در .env بررسی کنید.")
        if resp.status_code == 404:
            raise LLMError("آدرس یا نام مدل پیدا نشد. BASE_URL و MODEL را بررسی کنید.")
        if resp.status_code == 429:
            raise LLMError("سقف درخواست API پر شده است؛ کمی بعد دوباره امتحان کنید.")
        if resp.status_code >= 400:
            raise LLMError(f"سرویس مدل خطای {resp.status_code} برگرداند.")

    async def complete(self, messages: Messages, json_mode: bool = False,
                       temperature: float = 0.2) -> str:
        self._check_config()
        payload: dict[str, Any] = {
            "model": self.model, "messages": messages,
            "temperature": temperature, "stream": False,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                url = f"{self.base_url}/chat/completions"
                if json_mode:
                    payload["response_format"] = {"type": "json_object"}
                resp = await client.post(url, json=payload, headers=self._headers())
                if json_mode and resp.status_code in (400, 422):
                    # Some providers do not support response_format: retry without it.
                    payload.pop("response_format", None)
                    resp = await client.post(url, json=payload, headers=self._headers())
                self._raise_for_status(resp)
                data = resp.json()
            return data["choices"][0]["message"]["content"] or ""
        except LLMError:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise LLMUnavailable("اتصال به سرویس مدل برقرار نشد. اینترنت/ و BASE_URL را بررسی کنید.") from exc
        except httpx.TimeoutException as exc:
            raise LLMError("سرویس مدل دیر پاسخ داد (زمان انتظار تمام شد).") from exc
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError("پاسخ سرویس مدل قالب انتظار‌رفته را نداشت.") from exc

    async def stream(self, messages: Messages, temperature: float = 0.2) -> AsyncIterator[str]:
        self._check_config()
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature, "stream": True}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream("POST", f"{self.base_url}/chat/completions",
                                         json=payload, headers=self._headers()) as resp:
                    if resp.status_code >= 400:
                        await resp.aread()
                        self._raise_for_status(resp)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        body = line[5:].strip()
                        if body == "[DONE]":
                            break
                        try:
                            delta = json.loads(body)["choices"][0].get("delta", {})
                        except (ValueError, KeyError, IndexError):
                            continue
                        piece = delta.get("content")
                        if piece:
                            yield piece
        except LLMError:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise LLMUnavailable("اتصال به سرویس مدل برقرار نشد.") from exc
        except httpx.TimeoutException as exc:
            raise LLMError("سرویس مدل دیر پاسخ داد.") from exc

    async def ping(self) -> tuple[bool, str]:
        if not self.base_url or not self.model:
            return False, "تنظیمات BASE_URL و MODEL در فایل .env کامل نیست."
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(f"{self.base_url}/models", headers=self._headers())
        except Exception:  # noqa: BLE001
            return False, "اتصال به سرویس مدل برقرار نشد (اینترنت یا  را بررسی کنید)."
        if resp.status_code in (401, 403):
            return False, "کلید API نامعتبر است."
        # Some providers do not implement /models (404): reachable is good enough.
        return True, "سرویس مدل در دسترس است."


def build_llm(settings: Settings) -> BaseLLM:
    if settings.llm_provider == "openai_compatible":
        return OpenAICompatibleLLM(settings.base_url, settings.api_key,
                                   settings.model, settings.llm_timeout)
    return OllamaLLM(settings.ollama_host, settings.ollama_model, settings.llm_timeout)
