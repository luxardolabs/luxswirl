"""Rendering for notification emails.

Email bodies are templates under app/templates/email/, not markup built in
Python. Autoescape is on because every value in an alert email comes from
outside: a check's name and target are user input, and its error message is
whatever the monitored service answered. StrictUndefined makes a missing value
fail the render instead of sending a blank field.
"""

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_EMAIL_TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "email"

_env = Environment(
    loader=FileSystemLoader(str(_EMAIL_TEMPLATES)),
    autoescape=True,
    undefined=StrictUndefined,
)


def render_email(template_name: str, **context: Any) -> str:
    """Render an email template with `context`, every value HTML-escaped."""
    return _env.get_template(template_name).render(**context)
