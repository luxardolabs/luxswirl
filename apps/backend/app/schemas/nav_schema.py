"""
Navigation schema - the app-wide links every full page renders.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class NavUrls:
    """Finished hrefs for the shared layout: the sidebar, the settings sub-nav,
    the sign-in/password forms and the list pages that link to themselves.

    Built once per render from the route NAMES (fw.urls_from_route_names), so a
    router path change reaches every link without a template edit. A frozen
    dataclass, not a dict: `{{ nav.update }}` on a dict would render the bound
    `dict.update` method (tests/test_url_bundles.py).
    """

    home: str
    dashboard: str
    jobs: str
    status_pages: str
    notification_logs: str
    agents: str
    settings: str
    profile: str
    scheduler: str
    database_health: str
    login: str
    logout: str
    change_password: str
    settings_notifications: str
    settings_alerts: str
    settings_registration_keys: str
    settings_defaults: str
    settings_users: str
    settings_api_activity: str
    settings_components: str
    settings_logo_demo: str
