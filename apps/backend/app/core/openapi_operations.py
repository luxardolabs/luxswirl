"""An API operation's OpenAPI id and default summary come from its handler, not its route name.

FastAPI builds each operation's `operationId`, and its `summary` when the route
sets none, from the route's NAME. Route names are `[area.]<resource>.<action>`
(`api.admin.agents.index`), the shape `url_path_for` resolves by, while a client
generated from the admin API's OpenAPI document takes its method names from the
operationIds. Deriving both from the handler function (`admin_list_agents`)
keeps the published document stable whatever the route is named.

`operation_id` is the app's `generate_unique_id_function`; every router that
serves API routes declares `route_class=HandlerNamedRoute`.
"""

import re
from collections.abc import Callable
from typing import Any

from fastapi.routing import APIRoute


def operation_id(route: APIRoute) -> str:
    """FastAPI's `generate_unique_id`, keyed on the handler's name instead of the route's."""
    if not route.methods:
        raise ValueError(f"API route {route.path_format} declares no HTTP method")
    operation = re.sub(r"\W", "_", f"{route.endpoint.__name__}{route.path_format}")
    method = next(iter(route.methods)).lower()
    return f"{operation}_{method}"


class HandlerNamedRoute(APIRoute):
    """An API route whose summary, when it sets none, is its handler's name, title-cased."""

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        if kwargs.get("summary") is None:
            kwargs["summary"] = endpoint.__name__.replace("_", " ").title()
        super().__init__(path, endpoint, **kwargs)
