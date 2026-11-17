"""Public response types, included in OpenAPI for the frontend owner."""
from typing import Literal
from pydantic import AwareDatetime, BaseModel, Field


class AppliedFilter(BaseModel):
    start: AwareDatetime = Field(alias='from')
    until: AwareDatetime | None
    after_local_minute: int | None
    evening_from: str | None
    timezone: str


class SearchResult(BaseModel):
    event_id: str
    event_version: int
    title: str
    description: str
    tags: list[str]
    starts_at: AwareDatetime
    score: float
    indexed_at: AwareDatetime | None


class SearchResponse(BaseModel):
    mode: Literal['semantic', 'keyword_fallback']
    label: str
    fallback_reason: str | None
    filter: AppliedFilter
    index_version: str
    model: str
    model_version: str
    embedding_provider: Literal['ollama', 'simulated', 'simulated_http']
    results: list[SearchResult]
