"""
Status pages view service — context building for the
status-page admin and public-view endpoints.

Wraps StatusPageCoreService (core) for status-page CRUD and orchestrates
the dashboard rendering view-model assembly via DashboardRender.
"""

import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.query_params import split_csv
from app.models.enum_model import CheckHealthStatus, CheckType, MaintenanceJobKind
from app.schemas.auth_schema import AuthedUser
from app.schemas.maintenance_job_schema import MaintenanceJobResponse
from app.schemas.pagination_schema import build_pagination
from app.schemas.status_page_schema import (
    DashboardItemUrls,
    StatusPageCreate,
    StatusPageInDB,
    StatusPageRow,
    StatusPageUpdate,
    StatusPageUrls,
)
from app.services.core.check_core_service import CheckCoreService
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.services.core.status_page_core_service import StatusPageCoreService
from app.services.views._dashboard_render import DashboardRender
from app.shared.logger import get_logger

logger = get_logger("luxswirl.web.services.status_pages")

# Status-bar windows surfaced to the UI. Used to validate user input on
# create/update, and to resolve the time_range query param in the public view.
ALLOWED_TIME_RANGES: list[int] = [30, 60, 120, 240, 480, 1440]


def _normalize_status_bar_minutes(minutes: int | None) -> int:
    """Coerce status_bar_minutes to one of the allowed windows; default 30."""
    if minutes in ALLOWED_TIME_RANGES:
        return minutes
    return 30


# The label each allowed range is shown as in the selector.
TIME_RANGE_LABELS: dict[int, str] = {
    30: "30m",
    60: "1h",
    120: "2h",
    240: "4h",
    480: "8h",
    1440: "24h",
}


def _time_range_choices(base_url: str, *, selected: int) -> list[dict[str, object]]:
    """The public page's time-range selector, ready to render.

    Args:
        base_url: The public status page path the options link to.
        selected: The range currently in effect.

    Returns:
        One entry per allowed range with its label, href and current flag.
    """
    return [
        {
            "minutes": minutes,
            "label": TIME_RANGE_LABELS[minutes],
            "url": f"{base_url}?time_range={minutes}",
            "is_current": minutes == selected,
        }
        for minutes in ALLOWED_TIME_RANGES
    ]


def _resolve_time_range(
    query_value: int | None, page_config: dict[str, Any] | None
) -> int:
    """Pick time-range minutes: query > page config > default 30."""
    if query_value is not None and query_value in ALLOWED_TIME_RANGES:
        return query_value
    config = page_config or {}
    cfg_minutes = config.get("status_bar_minutes", 30)
    return cfg_minutes if cfg_minutes in ALLOWED_TIME_RANGES else 30


def build_status_page_urls(
    request: Request, page_id: UUID, slug: str
) -> StatusPageUrls:
    # .path, not str(): url_for returns an absolute url built from the request's
    # scheme/host, which behind TLS-terminating nginx can emit http:// into https://.
    def u(name: str, **kw: object) -> str:
        return request.url_for(name, **kw).path

    return StatusPageUrls(
        edit_form=u("status_page_edit_form", status_page_id=page_id),
        manage=u("manage_status_page", status_page_id=page_id),
        delete=u("delete_status_page", status_page_id=page_id),
        update=u("update_status_page", status_page_id=page_id),
        available_checks=u("get_available_checks", status_page_id=page_id),
        public=u("view_public_status_page", slug=slug),
        reorder=u("reorder_dashboard_items", status_page_id=page_id),
        add_check=u("add_check_to_dashboard", status_page_id=page_id),
        add_group=u("add_group_to_dashboard", status_page_id=page_id),
    )


def _dashboard_item_urls(
    request: Request, status_page_id: UUID, index: int
) -> DashboardItemUrls:
    """The item routes for the item at `index`, resolved by route name."""

    def u(name: str, **kw: object) -> str:
        return str(request.app.url_path_for(name, status_page_id=status_page_id, **kw))

    return DashboardItemUrls(
        remove=u("remove_item_from_dashboard", item_index=index),
        rename_group=u("rename_group_in_dashboard", item_index=index),
        update_group_filters=u("update_group_filters_in_dashboard", item_index=index),
        sort=u("update_group_sort_in_dashboard", item_index=index),
    )


def _dashboard_items(
    request: Request, status_page: StatusPageInDB
) -> list[tuple[dict[str, Any], DashboardItemUrls]]:
    """Each dashboard item paired with its finished hrefs.

    The item routes address an item by its position, so the pairing is built
    here, in `status_page.items` order, rather than looked up by loop index in
    the template (or spelled into the manage page's script).
    """
    return [
        (item, _dashboard_item_urls(request, status_page.id, index))
        for index, item in enumerate(status_page.items or [])
    ]


class StatusPagesViewService:
    """View-layer wrapper for /status-pages and /status/{slug} endpoints."""

    # ---- list / forms / manage page contexts -------------------------------

    @staticmethod
    async def build_list_context(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        is_public: str,
        page: int,
        per_page: int | None,
    ) -> dict[str, Any]:
        """Status-pages index (paginated)."""
        if per_page is None:
            per_page = await SettingsCoreService.get_setting(
                db, "general.default_page_size", 50
            )
        offset = (page - 1) * per_page

        is_public_filter: bool | None = None
        if is_public == "true":
            is_public_filter = True
        elif is_public == "false":
            is_public_filter = False

        status_pages, total = await StatusPageCoreService.list_status_pages(
            db=db, is_public=is_public_filter, limit=per_page, offset=offset
        )
        public_count = await StatusPageCoreService.get_public_count(db)
        private_count = await StatusPageCoreService.get_private_count(db)

        filters = {"is_public": is_public}
        pagination = build_pagination(
            page=page, per_page=per_page, total=total, filters=filters
        )
        return {
            "request": request,
            "current_user": current_user,
            "status_pages": [
                StatusPageRow(
                    **dict(sp), urls=build_status_page_urls(request, sp.id, sp.slug)
                )
                for sp in status_pages
            ],
            "public_count": public_count,
            "private_count": private_count,
            "filters": filters,
            "pagination": pagination,
            "page_title": "Status Pages",
            # The page's own path: filters and pagination re-request it.
            "list_url": str(request.app.url_path_for("status_pages_list")),
            "create_form_url": str(request.app.url_path_for("status_page_create_form")),
        }

    @staticmethod
    def build_create_form_context(
        request: Request, current_user: AuthedUser
    ) -> dict[str, Any]:
        """Empty status-page form."""
        return {
            "request": request,
            "current_user": current_user,
            "status_page": None,
            "create_url": str(request.app.url_path_for("create_status_page")),
        }

    @staticmethod
    async def build_edit_form_context(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        status_page_id: UUID,
    ) -> dict[str, Any]:
        """Populated status-page form (raises StatusPageNotFoundException if missing)."""
        status_page = await StatusPageCoreService.get_status_page(db, status_page_id)
        return {
            "sp_urls": build_status_page_urls(
                request, status_page.id, status_page.slug
            ),
            "request": request,
            "current_user": current_user,
            "status_page": status_page,
        }

    @staticmethod
    async def build_manage_context(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        status_page_id: UUID,
    ) -> dict[str, Any]:
        """Full manage page — items, filters, available checks."""
        status_page = await StatusPageCoreService.get_status_page(db, status_page_id)
        context = await DashboardRender.build_dashboard_context(db, status_page)
        all_tags = await CheckCoreService.get_all_tags_combined(db)
        filtered_checks = await DashboardRender.get_filtered_checks_with_status(
            db=db, limit=1000
        )
        dashboard_check_ids = DashboardRender.extract_check_ids_from_items(
            status_page.items
        )

        return {
            "sp_urls": build_status_page_urls(
                request, status_page.id, status_page.slug
            ),
            "request": request,
            "current_user": current_user,
            "status_page": status_page,
            "dashboard_items": _dashboard_items(request, status_page),
            "filtered_checks": filtered_checks,
            "dashboard_check_ids": set(dashboard_check_ids),
            **context,
            "all_tags": all_tags,
            "page_title": f"Manage: {status_page.name}",
        }

    @staticmethod
    async def build_available_checks_context(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        status_page_id: UUID,
        agent_id: UUID | None,
        check_type: CheckType | None,
        status: CheckHealthStatus | None,
        tags: str,
        search: str,
    ) -> dict[str, Any]:
        """Available-checks filter partial."""
        filtered_checks = await DashboardRender.get_filtered_checks_with_status(
            db=db,
            agent_id=agent_id or None,
            check_type=check_type or None,
            status=status or None,
            tags=split_csv(tags),
            search=search or None,
            limit=1000,
        )
        status_page = await StatusPageCoreService.get_status_page(db, status_page_id)
        dashboard_check_ids = [
            item.get("check_id")
            for item in status_page.items
            if item.get("type") == "check" and item.get("check_id")
        ]
        return {
            "request": request,
            "current_user": current_user,
            "filtered_checks": filtered_checks,
            "dashboard_check_ids": set(dashboard_check_ids),
        }

    @staticmethod
    async def build_dashboard_items_context(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        status_page_id: UUID,
    ) -> dict[str, Any]:
        """Dashboard items partial (used after filter changes)."""
        status_page = await StatusPageCoreService.get_status_page(db, status_page_id)
        context = await DashboardRender.build_dashboard_context(db, status_page)
        all_tags = await CheckCoreService.get_all_tags_combined(db)
        return {
            "request": request,
            "current_user": current_user,
            "status_page": status_page,
            "dashboard_items": _dashboard_items(request, status_page),
            **context,
            "all_tags": all_tags,
        }

    # ---- mutations: create / update / delete -------------------------------

    @staticmethod
    async def create_status_page(
        db: AsyncSession,
        name: str,
        slug: str,
        description: str,
        is_public: bool,
        status_bar_minutes: int,
    ) -> StatusPageInDB:
        """Create a status page (validates status_bar_minutes)."""
        config = {
            "status_bar_minutes": _normalize_status_bar_minutes(status_bar_minutes)
        }
        data = StatusPageCreate(
            name=name,
            slug=slug,
            description=description if description else None,
            is_public=is_public,
            config=config,
            items=[],
        )
        status_page = await StatusPageCoreService.create_status_page(db, data)
        logger.info(
            "Created status page",
            extra={"slug": status_page.slug, "status_page_id": str(status_page.id)},
        )
        return status_page

    @staticmethod
    async def update_status_page(
        db: AsyncSession,
        status_page_id: UUID,
        name: str,
        slug: str,
        description: str,
        is_public: bool,
        status_bar_minutes: int,
    ) -> StatusPageInDB:
        """Update a status page (merges existing config with new status_bar_minutes)."""
        existing_page = await StatusPageCoreService.get_status_page(db, status_page_id)
        existing_config = (existing_page.config or {}) if existing_page else {}
        existing_config["status_bar_minutes"] = _normalize_status_bar_minutes(
            status_bar_minutes
        )

        data = StatusPageUpdate(
            name=name,
            slug=slug,
            description=description if description else None,
            is_public=is_public,
            config=existing_config,
        )
        updated_page = await StatusPageCoreService.update_status_page(
            db, status_page_id, data
        )
        logger.info(
            "Updated status page",
            extra={"slug": updated_page.slug, "status_page_id": str(updated_page.id)},
        )
        return updated_page

    @staticmethod
    async def enqueue_delete(
        db: AsyncSession, status_page_id: UUID, owner_id: UUID | None = None
    ) -> MaintenanceJobResponse:
        """Enqueue a status_page_delete maintenance job (the cascade runs in the worker)."""
        await StatusPageCoreService.get_status_page(db, status_page_id)  # 404 cleanly
        return await MaintenanceJobCoreService.enqueue(
            db,
            kind=MaintenanceJobKind.STATUS_PAGE_DELETE,
            target_id=status_page_id,
            owner_id=owner_id,
        )

    @staticmethod
    async def delete_status_page(db: AsyncSession, status_page_id: UUID) -> None:
        """Delete a status page."""
        await StatusPageCoreService.delete_status_page(db, status_page_id)
        logger.info(
            "Deleted status page",
            extra={"status_page_id": str(status_page_id)},
        )

    # ---- mutations: dashboard items ----------------------------------------

    @staticmethod
    async def add_check_to_dashboard(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        status_page_id: UUID,
        check_id: UUID,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """
        Add a check to the dashboard.

        Returns (context, error). Error string: "no_items" (server-side issue
        after add). check_id is UUID-validated at the router boundary.
        On success returns a context dict for the dashboard_item_single partial.
        """
        status_page = await StatusPageCoreService.add_check_to_page(
            db, status_page_id, check_id
        )
        logger.info(
            "Added check to status page",
            extra={
                "check_id": str(check_id),
                "status_page_id": str(status_page_id),
            },
        )

        if not status_page.items:
            logger.error("No items in status page after adding check")
            return None, "no_items"

        context = await DashboardRender.build_dashboard_context(db, status_page)
        return {
            "request": request,
            "current_user": current_user,
            "status_page": status_page,
            "dashboard_items": _dashboard_items(request, status_page),
            **context,
        }, None

    @staticmethod
    async def remove_item_from_dashboard(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        status_page_id: UUID,
        item_index: int,
    ) -> dict[str, Any]:
        """Remove an item, return context for dashboard_items partial refresh."""
        status_page = await StatusPageCoreService.remove_item_from_page(
            db, status_page_id, item_index
        )
        logger.info(
            "Removed item from status page",
            extra={"item_index": item_index, "status_page_id": str(status_page_id)},
        )
        context = await DashboardRender.build_dashboard_context(db, status_page)
        return {
            "request": request,
            "current_user": current_user,
            "status_page": status_page,
            "dashboard_items": _dashboard_items(request, status_page),
            **context,
        }

    @staticmethod
    async def reorder_dashboard_items(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        status_page_id: UUID,
        items_json: str,
    ) -> dict[str, Any]:
        """Reorder dashboard items, return refreshed dashboard_items context."""
        items = json.loads(items_json)
        await StatusPageCoreService.reorder_all_items(db, status_page_id, items)
        logger.info(
            "Reordered items in status page",
            extra={"status_page_id": str(status_page_id)},
        )
        status_page = await StatusPageCoreService.get_status_page(db, status_page_id)
        context = await DashboardRender.build_dashboard_context(db, status_page)
        return {
            "request": request,
            "current_user": current_user,
            "status_page": status_page,
            "dashboard_items": _dashboard_items(request, status_page),
            **context,
        }

    @staticmethod
    async def add_group_to_dashboard(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        status_page_id: UUID,
        name: str,
        filter_json: str | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Add a group; returns (context, error)."""
        name = name or "New Group"
        filter = json.loads(filter_json) if filter_json is not None else None

        status_page = await StatusPageCoreService.add_group_to_page(
            db, status_page_id, name, filter
        )
        logger.info(
            "Added container group to status page",
            extra={
                "group_name": name,
                "status_page_id": str(status_page_id),
            },
        )

        if not status_page.items:
            logger.error("No items in status page after adding group")
            return None, "no_items"

        context = await DashboardRender.build_dashboard_context(db, status_page)
        return {
            "request": request,
            "current_user": current_user,
            "status_page": status_page,
            "dashboard_items": _dashboard_items(request, status_page),
            **context,
        }, None

    @staticmethod
    async def rename_group(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        status_page_id: UUID,
        item_index: int,
        new_name: str,
    ) -> dict[str, Any]:
        """Rename a group, return refreshed dashboard_items context."""
        status_page = await StatusPageCoreService.rename_group(
            db, status_page_id, item_index, new_name
        )
        logger.info(
            "Renamed group in status page",
            extra={
                "item_index": item_index,
                "new_name": new_name,
                "status_page_id": str(status_page_id),
            },
        )
        context = await DashboardRender.build_dashboard_context(db, status_page)
        return {
            "request": request,
            "current_user": current_user,
            "status_page": status_page,
            "dashboard_items": _dashboard_items(request, status_page),
            **context,
        }

    @staticmethod
    async def update_group_filters(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        status_page_id: UUID,
        item_index: int,
        filter_json: str,
    ) -> dict[str, Any]:
        """Update filters for a dynamic filter group, return dashboard_items context."""
        filter = json.loads(filter_json) if filter_json else {}
        await StatusPageCoreService.update_group_filters(
            db, status_page_id, item_index, filter
        )
        logger.info(
            "Updated filters for group in status page",
            extra={
                "item_index": item_index,
                "status_page_id": str(status_page_id),
            },
        )
        status_page = await StatusPageCoreService.get_status_page(db, status_page_id)
        context = await DashboardRender.build_dashboard_context(db, status_page)
        return {
            "request": request,
            "current_user": current_user,
            "status_page": status_page,
            "dashboard_items": _dashboard_items(request, status_page),
            **context,
        }

    @staticmethod
    async def update_group_sort(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        status_page_id: UUID,
        item_index: int,
        sort_by: str,
        sort_direction: str,
    ) -> dict[str, Any]:
        """Update group sort settings, return refreshed dashboard_items context."""
        status_page = await StatusPageCoreService.update_group_sort(
            db, status_page_id, item_index, sort_by, sort_direction
        )
        logger.info(
            "Updated sort for group in status page",
            extra={
                "item_index": item_index,
                "status_page_id": str(status_page_id),
                "sort_by": sort_by,
                "sort_direction": sort_direction,
            },
        )
        context = await DashboardRender.build_dashboard_context(db, status_page)
        all_tags = await CheckCoreService.get_all_tags_combined(db)
        return {
            "request": request,
            "current_user": current_user,
            "status_page": status_page,
            "dashboard_items": _dashboard_items(request, status_page),
            **context,
            "all_tags": all_tags,
        }

    # ---- public view (GET /status/{slug} and partial) ----------------------

    @staticmethod
    def public_login_redirect_url(request: Request, slug: str) -> str:
        """Sign-in href for a private status page, returning to that page after.

        Both paths come from the route table by name, so neither the login page
        nor the public page's path has a second home here.
        """
        login = request.app.url_path_for("login_page")
        page = request.app.url_path_for("view_public_status_page", slug=slug)
        return f"{login}?{urlencode({'redirect': page})}"

    @staticmethod
    async def build_public_view(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser | None,
        slug: str,
        time_range: int | None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """
        Resolve a public status page by slug.

        Returns (context, signal). signal ∈ {None, "not_found", "redirect_login", error_str}.
          - context populated, signal None → render full page
          - context None, signal "not_found" → 404
          - context None, signal "redirect_login" → 307 to login
        """
        status_page = await StatusPageCoreService.get_page_by_slug(db, slug)
        if not status_page:
            return None, "not_found"
        if not status_page.is_public and not current_user:
            return None, "redirect_login"

        time_range_minutes = _resolve_time_range(time_range, status_page.config)
        dashboard_data = await DashboardRender.render_public_dashboard(
            db, status_page, time_range_minutes=time_range_minutes
        )
        return {
            "request": request,
            "current_user": current_user,
            "status_page": status_page,
            **{
                k: dashboard_data[k]
                for k in (
                    "overall_status",
                    "overall_uptime",
                    "total_checks",
                    "checks_up",
                    "checks_down",
                    "rendered_items",
                    "time_range_minutes",
                    "time_range_label",
                    "num_bars",
                    "bucket_minutes",
                )
            },
            "page_title": status_page.name,
            # One finished choice per time-range option — label, href and whether
            # it is the current one — so the selector only renders.
            "time_range_choices": _time_range_choices(
                request.url_for("view_public_status_page", slug=status_page.slug).path,
                selected=time_range_minutes,
            ),
            "partial_url_base": request.url_for(
                "view_public_status_page_partial", slug=status_page.slug
            ).path,
        }, None

    @staticmethod
    async def build_public_view_partial(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser | None,
        slug: str,
        time_range: int | None,
    ) -> tuple[dict[str, Any] | None, int | None]:
        """
        Same as build_public_view but for HTMX polling partial.

        Returns (context, http_status). status is set on error (404, 401),
        None on success.
        """
        status_page = await StatusPageCoreService.get_page_by_slug(db, slug)
        if not status_page:
            return None, 404
        if not status_page.is_public and not current_user:
            return None, 401

        time_range_minutes = _resolve_time_range(time_range, status_page.config)
        dashboard_data = await DashboardRender.render_public_dashboard(
            db, status_page, time_range_minutes=time_range_minutes
        )
        return {
            "request": request,
            "current_user": current_user,
            "status_page": status_page,
            **{
                k: dashboard_data[k]
                for k in (
                    "overall_status",
                    "overall_uptime",
                    "total_checks",
                    "checks_up",
                    "checks_down",
                    "rendered_items",
                    "time_range_minutes",
                    "time_range_label",
                    "num_bars",
                    "bucket_minutes",
                )
            },
        }, None

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def build_error_partial_context(
        request: Request, current_user: AuthedUser | None, error: str
    ) -> dict[str, Any]:
        """Common error-partial context."""
        return {
            "request": request,
            "current_user": current_user,
            "error": error,
        }
