"""Result envelopes returned across every tool boundary.

A bare list cannot say whether it is the whole answer.  ``Page`` can, and both
``complete`` and ``next_cursor`` are required so no caller can forget to say.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

__all__ = ["Page"]

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """One bounded slice of a collection, honest about what it left out."""

    items: list[T] = Field(description="The items in this slice, at most `limit` of them.")
    complete: bool = Field(
        description=(
            "True when this page ends the result set: nothing further remains. "
            "False means the answer was cut short and `next_cursor` carries the rest."
        ),
    )
    next_cursor: str | None = Field(
        description=(
            "Opaque cursor for the remainder, or null when there is none. "
            "Pass it back verbatim; never parse it."
        ),
    )

    @classmethod
    def whole(cls, items: list[T]) -> "Page[T]":
        """A page that is provably the entire result set."""
        return cls(items=items, complete=True, next_cursor=None)
