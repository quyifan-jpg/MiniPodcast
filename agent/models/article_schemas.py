from pydantic import BaseModel, ConfigDict, field_validator
from typing import Optional, List, Dict, Any
from datetime import datetime


class ArticleBase(BaseModel):
    title: str
    url: Optional[str] = None
    published_date: str

    @field_validator("published_date", mode="before")
    @classmethod
    def coerce_datetime(cls, v):
        if isinstance(v, datetime):
            return v.isoformat()
        return v
    summary: Optional[str] = None
    content: Optional[str] = None
    categories: Optional[List[str]] = []
    source_name: Optional[str] = None


class Article(ArticleBase):
    id: int
    metadata: Optional[Dict[str, Any]] = {}

    model_config = ConfigDict(from_attributes=True)


class PaginatedArticles(BaseModel):
    items: List[Article]
    total: int
    page: int
    per_page: int
    total_pages: int
    has_next: bool
    has_prev: bool