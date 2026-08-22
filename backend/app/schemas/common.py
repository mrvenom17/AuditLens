"""Shared response primitives.

06_ENGINEERING_RULES.md § Type Safety: every route input and output has a
Pydantic schema, and no raw dict crosses a layer boundary.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    """Base for schemas built from ORM rows.

    Response schemas are declared field by field rather than derived from the
    model, so adding a Sensitive column to a table can never silently start
    returning it over the wire (03_DATA_MODEL.md §8.4).
    """

    model_config = ConfigDict(from_attributes=True)


class Page[T](BaseModel):
    items: list[T]
    total: int


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    """Documents the standard envelope in OpenAPI (02_ARCHITECTURE.md §7.7)."""

    error: ErrorDetail
