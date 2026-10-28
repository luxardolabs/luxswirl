"""Nav view service — the shared layout's links, resolved per request.

Every full page extends `layouts/base.html`, which includes the sidebar; the
settings pages add the settings sub-nav. Those links are the same on every
page, so they are built here, once, and handed to every TemplateResponse by the
template instance's context processor (`app/web/templates.py`). The template
renders finished strings (`{{ nav.agents }}`); it never names a route.
"""

from typing import Any

from fastapi import Request

from app.schemas.nav_schema import NavUrls


class NavViewService:
    """Builds the `nav` bundle every page renders."""

    @staticmethod
    def build_nav_urls(request: Request) -> NavUrls:
        """Resolve the shared layout's links by route name.

        `url_path_for`, not `url_for`: the relative path, so a TLS-terminating
        proxy cannot turn the href into an `http://` link on an `https://` page.

        Args:
            request: The request being rendered (its app holds the route table).

        Returns:
            The finished hrefs.
        """

        def u(name: str) -> str:
            return str(request.app.url_path_for(name))

        return NavUrls(
            home=u("root_redirect"),
            dashboard=u("status_page"),
            jobs=u("jobs_page"),
            status_pages=u("status_pages_list"),
            notification_logs=u("notification_logs_page"),
            agents=u("agents_list_page"),
            settings=u("settings_landing"),
            profile=u("profile_panel"),
            scheduler=u("scheduler_page"),
            database_health=u("database_health_page"),
            login=u("login_submit"),
            logout=u("web_logout"),
            change_password=u("change_password_submit"),
            settings_notifications=u("settings_notifications"),
            settings_alerts=u("settings_alerts"),
            settings_registration_keys=u("settings_registration_keys"),
            settings_defaults=u("settings_defaults"),
            settings_users=u("users_page"),
            settings_api_activity=u("api_activity_page"),
            settings_components=u("settings_components"),
            settings_logo_demo=u("settings_logo_demo"),
        )

    @staticmethod
    def nav_context(request: Request) -> dict[str, Any]:
        """Jinja2Templates context processor: `nav` on every TemplateResponse."""
        return {"nav": NavViewService.build_nav_urls(request)}
