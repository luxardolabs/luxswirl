"""
Canonical pagination state for every paginated page.

Adapted from the luxtaste portal pattern: single Pagination DTO produced by
`build_pagination`, rendered uniformly by the `pagination_controls` macro.

Every paginated view service should:
1. Compute `total` from a count query.
2. Call `build_pagination(page=..., per_page=..., total=..., filters=...)`.
3. Pass the result into the template context as `pagination`.

Templates render via:
    {% from 'macros/tables.html' import pagination_controls %}
    {{ pagination_controls(pagination, '/your-page-route') }}

URL construction goes through the `paginated_url` Jinja global so filter +
page state are preserved identically across prev/next links — no template
ever concatenates query strings by hand.
"""

from urllib.parse import urlencode

from pydantic import BaseModel


class Pagination(BaseModel):
    """Pagination state for a single rendered page.

    `query_base` holds the page's filter state as a urlencoded string
    (without `page=` or `per_page=`). `paginated_url` appends those when
    building prev/next/numbered links.
    """

    page: int  # 1-indexed
    per_page: int
    total: int
    total_pages: int  # >= 1 even when total == 0 (avoids 'Page 1 of 0')
    range_start: int  # 1-indexed; 0 when total == 0
    range_end: int  # min(page * per_page, total)
    has_prev: bool
    has_next: bool
    prev_page: int | None  # None on first page
    next_page: int | None  # None on last page
    query_base: str  # filters serialized as 'k=v&k=v', no leading '?'


def build_pagination(
    *,
    page: int,
    per_page: int,
    total: int,
    filters: dict | None = None,
) -> Pagination:
    """Construct a Pagination from raw paging inputs.

    Call this exactly once per paginated view-service method. Never build
    Pagination fields by hand — centralization is the whole point.

    `filters` should contain only the page's filter state (status, search,
    role, etc.) — NOT `page` or `per_page`. Empty / None / 'all' values are
    skipped so URLs stay clean.

    Args:
        page: Current page (1-indexed). Clamped silently to >= 1.
        per_page: Items per page. Must be > 0.
        total: Total matching items across all pages.
        filters: Mapping of filter param name -> value.
    """
    if per_page <= 0:
        raise ValueError("per_page must be > 0")
    page = max(1, page)

    total_pages = max(1, -(-total // per_page))  # ceil div, min 1
    has_prev = page > 1
    has_next = page < total_pages
    prev_page = page - 1 if has_prev else None
    next_page = page + 1 if has_next else None

    if total <= 0:
        range_start = 0
        range_end = 0
    else:
        range_start = (page - 1) * per_page + 1
        range_end = min(page * per_page, total)

    # Serialize filters into a stable query string.
    # Skip empty / None / 'all' (the conventional "no filter" sentinel).
    qs_pairs: list[tuple[str, str]] = []
    if filters:
        for key in sorted(filters):  # sorted for stable URLs
            value = filters[key]
            if value is None or value == "" or value == "all":
                continue
            if isinstance(value, bool):
                qs_pairs.append((key, "true" if value else "false"))
            else:
                qs_pairs.append((key, str(value)))
    query_base = urlencode(qs_pairs)

    return Pagination(
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        range_start=range_start,
        range_end=range_end,
        has_prev=has_prev,
        has_next=has_next,
        prev_page=prev_page,
        next_page=next_page,
        query_base=query_base,
    )


def paginated_url(
    base_url: str,
    pagination: Pagination,
    page: int | None = None,
) -> str:
    """Build a URL for a given page against `base_url`.

    Single source of truth for paginated URL construction. Used by the
    `pagination_controls` macro and any other template that needs to link
    to a different page of the same paginated view.

    Args:
        base_url: Route path without query string (e.g. '/notification-logs').
        pagination: The current Pagination DTO.
        page: Page to link to. Defaults to the current page (useful for
              "refresh" links that preserve filter state).
    """
    target_page = page if page is not None else pagination.page
    parts: list[str] = []
    if pagination.query_base:
        parts.append(pagination.query_base)
    parts.append(f"page={target_page}")
    parts.append(f"per_page={pagination.per_page}")
    return f"{base_url}?{'&'.join(parts)}"
