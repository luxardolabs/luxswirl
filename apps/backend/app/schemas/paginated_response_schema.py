# luxarch:paginated-response asset v1 - DO NOT edit this marker line; it is how repo.emitted_assets_current knows your copy is current. Re-emit with `luxarch --emit paginated-response`.
"""The ONE JSON page envelope. `luxarch --emit paginated-response`.

Drop this in at `app/schemas/pagination_schema.py`. A JSON API route that returns one page of rows
returns `PaginatedResponse[ItemOut]`, or a named subclass the iOS contract can diff by name:

    class CustomerList(PaginatedResponse[CustomerOut]):
        pass

The core service fills it with `.of(items, pagination)`, where `pagination` comes from
`build_pagination(page=, per_page=, total=)` (`luxarch --emit pagination`). The page arithmetic then
happens once, for the JSON API and the HTMX tables alike, and nothing re-declares page meta
(`fw.paginated_responses_subclass_base`, `fw.no_hand_rolled_pagination`).

## Why this is emitted, and why page-based

`fw.paginated_responses_subclass_base` told every JSON list to subclass `PaginatedResponse`, and no
asset defined it. The one fleet description said "items/total/page/…", and the reference app carried
an offset-based `total/limit/offset/has_more` class. Two repos that each invent the class differ, and
the iOS contract inherits whichever was invented. Page-based by owner ruling (2026-10-10): it is the
shape `build_pagination` already computes for every HTMX table, so one function feeds both.

A client walks pages with `page` until `has_next` is false, or stops when it has loaded `total`
rows; both are always present.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, Self

from pydantic import BaseModel


class PageState(Protocol):
    """What `.of()` reads: `PaginationState` from `luxarch --emit pagination` has every field. A
    Protocol, not an `app.` import, so this file lands in any repo unchanged (`app.` is first-party
    where it lands and third-party in luxarch's tree, and the two import orders disagree)."""

    @property
    def total(self) -> int: ...
    @property
    def page(self) -> int: ...
    @property
    def per_page(self) -> int: ...
    @property
    def total_pages(self) -> int: ...
    @property
    def has_next(self) -> bool: ...


class PaginatedResponse[T](BaseModel):
    """One page of `T`, with the page state `build_pagination` computed for it."""

    items: list[T]
    total: int
    page: int
    per_page: int
    total_pages: int
    has_next: bool

    @classmethod
    def of(cls, items: Sequence[T], pagination: PageState) -> Self:
        """The envelope for `items`, the rows of the page `pagination` describes."""
        return cls(
            items=list(items),
            total=pagination.total,
            page=pagination.page,
            per_page=pagination.per_page,
            total_pages=pagination.total_pages,
            has_next=pagination.has_next,
        )
