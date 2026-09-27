"""Result envelopes returned across every tool boundary.

A bare list cannot say whether it is the whole answer.  ``Page`` can, and both
``complete`` and ``next_cursor`` are required so no caller can forget to say.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = ["Chunk", "Page"]


class Page[T](BaseModel):
    """One bounded slice of a collection, honest about what it left out."""

    items: list[T] = Field(
        description="The items in this slice, at most `limit` of them."
    )
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
    def whole(cls, items: list[T]) -> Page[T]:
        """A page that is provably the entire result set."""
        return cls(items=items, complete=True, next_cursor=None)


class Chunk(BaseModel):
    """One bounded segment of a long text, honest about what follows it (AD-4).

    The text counterpart of :class:`Page`: ``complete`` and ``next_cursor`` are
    required, so no tool returning text can forget to say it was cut.
    """

    text: str = Field(description="This segment of the text.")
    complete: bool = Field(
        description=(
            "True when this segment ends the text. False means it was cut: the "
            "text says where, and `next_cursor` carries the rest."
        )
    )
    next_cursor: str | None = Field(
        description="Opaque cursor for the rest, or null. Pass it back verbatim."
    )
