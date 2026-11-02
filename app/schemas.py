"""Pydantic v2 models for the API and for validating LLM JSON outputs."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Depth = Literal["quick", "standard", "deep"]
FindingStatus = Literal["pending", "verified", "rejected", "unchecked"]

TOPIC_MIN = 3
TOPIC_MAX = 500


class JobCreate(BaseModel):
    topic: str
    depth: Depth = "standard"

    @field_validator("topic")
    @classmethod
    def _check_topic(cls, value: str) -> str:
        value = " ".join(value.split())
        if not (TOPIC_MIN <= len(value) <= TOPIC_MAX):
            raise ValueError("موضوع باید بین ۳ تا ۵۰۰ نویسه باشد.")
        return value


class JobCreated(BaseModel):
    job_id: str


# ---------- LLM output validation ----------

class SubQuestion(BaseModel):
    question: str = Field(min_length=3)
    search_query: str = Field(min_length=2)

    @field_validator("question", "search_query")
    @classmethod
    def _strip(cls, value: str) -> str:
        return " ".join(value.split())


class RawFinding(BaseModel):
    claim: str = Field(min_length=3)
    evidence: str = Field(min_length=3)

    @field_validator("claim", "evidence")
    @classmethod
    def _strip(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("evidence")
    @classmethod
    def _cap(cls, value: str) -> str:
        return value[:300]


class Verdict(BaseModel):
    supported: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalize_conf(cls, value):  # accept 85 as 0.85
        try:
            number = float(value)
        except (TypeError, ValueError):
            return value
        if 1.0 < number <= 100.0:
            number = number / 100.0
        return number


# ---------- Runtime objects ----------

class Finding(BaseModel):
    id: int
    sub_index: int
    claim: str
    evidence: str
    source_url: str
    source_title: str = ""
    status: FindingStatus = "pending"
    confidence: float | None = None
    reason: str = ""


class HistoryItem(BaseModel):
    id: str
    topic: str
    depth: str
    created_at: str
    partial: bool
    stats: dict


class HistoryDetail(HistoryItem):
    report: str
    sources: list[dict]
