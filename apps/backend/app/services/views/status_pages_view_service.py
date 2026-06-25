"""
Status pages view service — context building for the
status-page admin and public-view endpoints.

Wraps StatusPageCoreService (core) for status-page CRUD and orchestrates
the dashboard rendering view-model assembly via DashboardRender.
"""

from typing import Any
from uuid import UUID

from fastapi import Request
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.query_params import split_csv
from app.models.enum_model import CheckHealthStatus, CheckType, MaintenanceJobKind
from app.models.user_model import User
from app.schemas.pagination_schema import build_pagination
from app.schemas.status_page_schema import StatusPageCreate, StatusPageUpdate
from app.services.core.check_core_service import CheckCoreService
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.services.core.status_page_core_service import StatusPageCoreService
from app.services.views._dashboard_render import DashboardRender

logger = get_logger("luxswirl.web.services.status_pages")

# Status-bar windows surfaced to the UI. Used to validate user input on
# create/update, and to resolve the time_range query param in the public view.
ALLOWED_TIME_RANGES: list[int] = [30, 60, 120, 240, 480, 1440]


def _normalize_status_bar_minutes(minutes: int | None) -> int:
    """Coerce status_bar_minutes to one of the allowed windows; default 30."""
    if minutes in ALLOWED_TIME_RANGES:
        return minutes  # type: ignore[return-value]
    return 30


def _resolve_time_range(query_value: int | None, page_config: dict | None) -> int:
    """Pick time-range minutes: query > page config > default 30."""
    if query_value is not None and query_value in ALLOWED_TIME_RANGES:
        return query_value
    config = page_config or {}
    cfg_minutes = config.get("status_bar_minutes", 30)
    return cfg_minutes if cfg_minutes in ALLOWED_TIME_RANGES else 30


class StatusPagesViewService:
    """View-layer wrapper for /status-pages and /status/{slug} endpoints."""

    # ---- list / forms / manage page contexts -------------------------------

    @staticmethod
    async def build_list_context(
        db: AsyncSession,
        request: Request,
        current_user: User,
        is_public: str,
        page: int,
        per_page: int | None,
    ) -> dict[str, Any]:
        """Status-pages index (paginated)."""
        if per_page is None:
            per_page = await SettingsCoreService.get_setting(db, "general.default_page_size", 50)
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
        pagination = build_pagination(page=page, per_page=per_page, total=total, filters=filters)
        return {
            "request": request,
            "current_user": current_user,
            "status_pages": status_pages,
            "public_count": public_count,
            "private_count": private_count,
            "filters": filters,
            "pagination": pagination,
            "page_title": "Status Pages",
        }

    @staticmethod
    def build_create_form_context(request: Request, current_user: User) -> dict[str, Any]:
        """Empty status-page form."""
        return {
            "request": request,
            "current_user": current_user,
            "status_page": None,
        }

    @staticmethod
    async def build_edit_form_context(
        db: AsyncSession, request: Request, current_user: User, status_page_id: UUID
    ) -> dict[str, Any]:
        """Populated status-page form (raises StatusPageNotFoundException if missing)."""
        status_page = await StatusPageCoreService.get_status_page_by_id(db, status_page_id)
        return {
            "request": request,
            "current_user": current_user,
            "status_page": status_page,
        }

    @staticmethod
    async def build_manage_context(
        db: AsyncSession, request: Request, current_user: User, status_page_id: UUID
    ) -> dict[str, Any]:
        """Full manage page — items, filters, available checks."""
        status_page = await StatusPageCoreService.get_status_page_by_id(db, status_page_id)
        context = await DashboardRender.build_dashboard_context(db, status_page)
        all_tags = await CheckCoreService.get_all_tags_combined(db)
        filtered_checks = await DashboardRender.get_filtered_checks_with_status(db=db, limit=1000)
        dashboard_check_ids = DashboardRender.extract_check_ids_from_items(status_page.items)

        return {
            "request": request,
            "current_user": current_user,
            "status_page": status_page,
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
        current_user: User,
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
        status_page = await StatusPageCoreService.get_status_page_by_id(db, status_page_id)
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
        db: AsyncSession, request: Request, current_user: User, status_page_id: UUID
    ) -> dict[str, Any]:
        """Dashboard items partial (used after filter changes)."""
        status_page = await StatusPageCoreService.get_status_page_by_id(db, status_page_id)
        context = await DashboardRender.build_dashboard_context(db, status_page)
        all_tags = await CheckCoreService.get_all_tags_combined(db)
        return {
            "request": request,
            "current_user": current_user,
            "status_page": status_page,
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
    ):
        """Create a status page (validates status_bar_minutes)."""
        config = {"status_bar_minutes": _normalize_status_bar_minutes(status_bar_minutes)}
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
    ):
        """Update a status page (merges existing config with new status_bar_minutes)."""
        existing_page = await StatusPageCoreService.get_status_page_by_id(db, status_page_id)
        existing_config = (existing_page.config or {}) if existing_page else {}
        existing_config["status_bar_minutes"] = _normalize_status_bar_minutes(status_bar_minutes)

        data = StatusPageUpdate(
            name=name,
            slug=slug,
            description=description if description else None,
            is_public=is_public,
            config=existing_config,
        )
        updated_page = await StatusPageCoreService.update_status_page(db, status_page_id, data)
        logger.info(
            "Updated status page",
            extra={"slug": updated_page.slug, "status_page_id": str(updated_page.id)},
        )
        return updated_page

    @staticmethod
    async def enqueue_delete(db: AsyncSession, status_page_id: UUID, owner_id: UUID | None = None):
        """Enqueue a status_page_delete maintenance job. See LUXSWIRL-105."""
        await StatusPageCoreService.get_status_page_by_id(db, status_page_id)  # 404 cleanly
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
        current_user: User,
        status_page_id: UUID,
        check_id: UUID,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """
        Add a check to the dashboard.

        Returns (context, error). Error string: "no_items" (server-side issue
        after add). check_id is UUID-validated at the router boundary.
        On success returns a context dict for the dashboard_item_single partial.
        """
        status_page = await StatusPageCoreService.add_check_to_page(db, status_page_id, check_id)
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
            **context,
        }, None

    @staticmethod
    async def remove_item_from_dashboard(
        db: AsyncSession,
        request: Request,
        current_user: User,
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
            **context,
        }

    @staticmethod
    async def reorder_dashboard_items(
        db: AsyncSession,
        request: Request,
        current_user: User,
        status_page_id: UUID,
        items: list,
    ) -> dict[str, Any]:
        """Reorder dashboard items, return refreshed dashboard_items context."""
        await StatusPageCoreService.reorder_all_items(db, status_page_id, items)
        logger.info(
            "Reordered items in status page",
            extra={"status_page_id": str(status_page_id)},
        )
        status_page = await StatusPageCoreService.get_status_page_by_id(db, status_page_id)
        context = await DashboardRender.build_dashboard_context(db, status_page)
        return {
            "request": request,
            "current_user": current_user,
            "status_page": status_page,
            **context,
        }

    @staticmethod
    async def add_group_to_dashboard(
        db: AsyncSession,
        request: Request,
        current_user: User,
        status_page_id: UUID,
        body: dict,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Add a group; returns (context, error)."""
        name = body.get("name", "New Group")
        filter = body.get("filter", None)

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
            **context,
        }, None

    @staticmethod
    async def rename_group(
        db: AsyncSession,
        request: Request,
        current_user: User,
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
            **context,
        }

    @staticmethod
    async def update_group_filters(
        db: AsyncSession,
        request: Request,
        current_user: User,
        status_page_id: UUID,
        item_index: int,
        filter: dict,
    ) -> dict[str, Any]:
        """Update filters for a dynamic filter group, return dashboard_items context."""
        await StatusPageCoreService.update_group_filters(db, status_page_id, item_index, filter)
        logger.info(
            "Updated filters for group in status page",
            extra={
                "item_index": item_index,
                "status_page_id": str(status_page_id),
            },
        )
        status_page = await StatusPageCoreService.get_status_page_by_id(db, status_page_id)
        context = await DashboardRender.build_dashboard_context(db, status_page)
        return {
            "request": request,
            "current_user": current_user,
            "status_page": status_page,
            **context,
        }

    @staticmethod
    async def update_group_sort(
        db: AsyncSession,
        request: Request,
        current_user: User,
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
            **context,
            "all_tags": all_tags,
        }

    # ---- public view (GET /status/{slug} and partial) ----------------------

    @staticmethod
    async def build_public_view(
        db: AsyncSession,
        request: Request,
        current_user: User | None,
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
        status_page = await StatusPageCoreService.get_status_page_by_slug(db, slug)
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
            "time_range_options": ALLOWED_TIME_RANGES,
            "page_title": status_page.name,
        }, None

    @staticmethod
    async def build_public_view_partial(
        db: AsyncSession,
        request: Request,
        current_user: User | None,
        slug: str,
        time_range: int | None,
    ) -> tuple[dict[str, Any] | None, int | None]:
        """
        Same as build_public_view but for HTMX polling partial.

        Returns (context, http_status). status is set on error (404, 401),
        None on success.
        """
        status_page = await StatusPageCoreService.get_status_page_by_slug(db, slug)
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
        request: Request, current_user: User | None, error: str
    ) -> dict[str, Any]:
        """Common error-partial context."""
        return {
            "request": request,
            "current_user": current_user,
            "error": error,
        }

    @staticmethod
    def build_error_page_context(
        request: Request,
        current_user: User | None,
        error: str,
        page_title: str = "Error",
    ) -> dict[str, Any]:
        """Common error-page context (full page, not partial)."""
        return {
            "request": request,
            "current_user": current_user,
            "error": error,
            "page_title": page_title,
        }
