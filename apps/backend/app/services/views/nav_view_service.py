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
            home=u("home.redirect"),
            dashboard=u("dashboard.index"),
            jobs=u("jobs.index"),
            status_pages=u("status_pages.index"),
            notification_logs=u("notification_logs.index"),
            agents=u("agents.index"),
            settings=u("settings.index"),
            profile=u("profile.show"),
            scheduler=u("scheduler.index"),
            database_health=u("database_health.index"),
            login=u("auth.login"),
            logout=u("auth.logout"),
            change_password=u("auth.change_password"),
            settings_notifications=u("settings.notifications"),
            settings_alerts=u("settings.alerts"),
            settings_registration_keys=u("settings.registration_keys"),
            settings_defaults=u("settings.defaults"),
            settings_users=u("users.index"),
            settings_api_activity=u("api_activity.index"),
            settings_components=u("settings.components"),
            settings_logo_demo=u("settings.logo_demo"),
        )

    @staticmethod
    def nav_context(request: Request) -> dict[str, Any]:
        """Jinja2Templates context processor: `nav` on every TemplateResponse."""
        return {"nav": NavViewService.build_nav_urls(request)}
