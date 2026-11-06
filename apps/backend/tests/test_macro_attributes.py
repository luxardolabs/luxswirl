"""A server value reaches a macro's tag only as an escaped attribute's value.

`attrs` is rendered through the app's strict `xmlattr`, so a value holding a quote stays inside its
attribute and adds none of its own. The JavaScript attributes the macros write are
constants that read `data-*`, so a value there never runs. Each test parses the
output as a browser would, and compares the attribute names and the decoded values.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.scan_panel import panel_result
from app.web.templates import templates
from jinja2 import UndefinedError
from markupsafe import Markup

pytestmark = pytest.mark.unit

HOSTILE = ['x" onmouseover="alert(1)', "'); alert(1); ('", "x' onfocus='alert(1)"]


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, attrs))


def _find(html: str, tag: str) -> list[dict[str, str | None]]:
    """Every `tag` in `html`, each attribute named once, as a browser keeps them."""
    parser = _Tags()
    parser.feed(html)
    found = []
    for name, attrs in parser.tags:
        if name == tag:
            kept: dict[str, str | None] = {}
            for key, value in attrs:
                kept.setdefault(key, value)
            found.append(kept)
    return found


def _one(html: str, tag: str) -> dict[str, str | None]:
    found = _find(html, tag)
    assert len(found) == 1, html
    return found[0]


def _macros(call: str, **context: Any) -> str:
    source = (
        '{% from "macros/buttons.html" import icon_btn, text_btn, tag_pill_button, '
        "toggle_switch, btn_save, segmented_option, confirm_action_btn, "
        "delete_icon_button %}"
        '{% from "macros/form_fields.html" import input, select %}'
        '{% from "macros/panels.html" import panel_wrapper, panel_header %}' + call
    )
    return templates.env.from_string(source).render(**context)


def _partial(name: str, **context: Any) -> str:
    return templates.env.get_template(name).render(**context)


@pytest.mark.parametrize("value", HOSTILE)
@pytest.mark.parametrize(
    ("call", "tag", "expected"),
    [
        (
            "{{ tag_pill_button('t', attrs={'data-tag': value}) }}",
            "button",
            {"type", "class", "data-tag"},
        ),
        (
            "{{ text_btn('', 'Go', type='button', attrs={'data-tag': value}) }}",
            "button",
            {"type", "class", "data-tag"},
        ),
        (
            "{{ text_btn('', 'Go', href='/x', attrs={'data-tag': value}) }}",
            "a",
            {"href", "class", "data-tag"},
        ),
        (
            "{{ btn_save(attrs={'data-tag': value}) }}",
            "button",
            {"type", "class", "data-tag"},
        ),
        (
            "{{ icon_btn('', attrs={'data-tag': value}) }}",
            "button",
            {"type", "class", "title", "data-tag"},
        ),
        (
            "{{ input(id=false, attrs={'data-tag': value}) }}",
            "input",
            {"type", "class", "data-tag"},
        ),
        (
            "{% call select(id=false, attrs={'data-tag': value}) %}{% endcall %}",
            "select",
            {"class", "data-tag"},
        ),
        (
            "{{ segmented_option('t', attrs={'data-tag': value}) }}",
            "button",
            {"type", "class", "data-tag"},
        ),
        (
            "{{ toggle_switch(true, attrs={'data-tag': value}) }}",
            "button",
            {
                "type",
                "role",
                "aria-checked",
                "class",
                "title",
                "data-tag",
                "hx-swap",
            },
        ),
    ],
)
def test_a_hostile_value_stays_inside_its_attribute(
    call: str, tag: str, expected: set[str], value: str
) -> None:
    element = _one(_macros(call, value=value), tag)

    assert set(element) == expected
    assert element["data-tag"] == value


def test_attrs_refuses_a_key_that_could_end_the_attribute() -> None:
    with pytest.raises(ValueError):
        _macros("{{ tag_pill_button('t', attrs={'a onclick=x': 'y'}) }}")


def test_attrs_comes_before_the_buttons_default_indicator() -> None:
    """The browser keeps an attribute's first value, so a caller's indicator wins."""
    html = _macros(
        "{{ text_btn('', 'Ping', type='button', hx_post='/x', "
        "attrs={'hx-indicator': '#mine'}) }}"
    )

    assert _one(html, "button")["hx-indicator"] == "#mine"


@pytest.mark.parametrize("value", HOSTILE)
def test_a_stored_tag_adds_no_attribute_to_its_pill(value: str) -> None:
    html = _partial(
        "partials/panels/agents/fields/tags.html",
        available_agent_tags=[value],
        agent=SimpleNamespace(tags=[]),
    )

    pill = _one(html.split('id="agent-tag-browser"')[1], "button")
    assert set(pill) == {"type", "class", "data-add-agent-tag"}
    assert pill["data-add-agent-tag"] == value


@pytest.mark.parametrize("value", HOSTILE)
def test_a_tab_url_adds_no_attribute_to_its_tab(value: str) -> None:
    tab = SimpleNamespace(label="t", active=False, url=value)
    html = _partial(
        "partials/profile/api_activity.html",
        surface="mcp",
        days=7,
        rows=[],
        row_limit=1,
        surface_tabs=[tab],
        day_tabs=[],
    )

    button = _one(html, "button")
    assert set(button) == {"type", "class", "hx-get", "hx-target", "hx-swap"}
    assert button["hx-get"] == value


@pytest.mark.parametrize("value", HOSTILE)
def test_a_metrics_token_stays_in_its_data_attributes(value: str) -> None:
    html = _partial("partials/settings/metrics_token.html", generated=True, token=value)

    copy, download = [
        b
        for b in _find(html, "button")
        if "data-copy-token" in b or "data-download-token" in b
    ]
    assert copy["data-copy-token"] == value
    assert copy["id"] == "copy-token-btn"
    assert download["data-download-token"] == value
    assert not [k for k in {**copy, **download} if k.startswith("on")]


@pytest.mark.parametrize("value", HOSTILE)
def test_hx_vals_carries_a_hostile_value_as_json(value: str) -> None:
    html = _macros(
        "{{ icon_btn('', hx_post='/x', hx_vals={'target': value, 'port': '80'}) }}",
        value=value,
    )

    button = _one(html, "button")
    assert not [k for k in button if k.startswith("on")]
    assert json.loads(button["hx-vals"] or "") == {"target": value, "port": "80"}


CONFIRM_ATTRS = {
    "type",
    "x-data",
    "@click",
    ":class",
    ":title",
    "aria-label",
    "hx-disabled-elt",
    "hx-indicator",
    "class",
    "data-ajax-method",
    "data-ajax-url",
    "data-ajax-target",
    "data-ajax-swap",
    "data-armed-class",
    "data-normal-class",
    "data-confirm-title",
    "data-confirm-timeout",
}


@pytest.mark.parametrize("value", HOSTILE)
def test_a_hostile_value_reaches_the_confirm_button_as_data(value: str) -> None:
    html = _macros(
        "{{ confirm_action_btn(hx_post=value, hx_target=value, hx_swap=value, "
        "title=value, timeout=value) }}",
        value=value,
    )

    button = _one(html, "button")
    assert set(button) == CONFIRM_ATTRS
    for name in (
        "data-ajax-url",
        "data-ajax-target",
        "data-ajax-swap",
        "data-confirm-title",
        "data-confirm-timeout",
    ):
        assert button[name] == value
    for name in ("x-data", "@click", ":class", ":title"):
        assert "alert" not in (button[name] or "")


def test_the_confirm_buttons_javascript_is_the_same_whatever_it_is_given() -> None:
    first = _one(
        _macros("{{ confirm_action_btn(hx_delete='/a', color='red', title='A') }}"),
        "button",
    )
    second = _one(
        _macros(
            "{{ confirm_action_btn(hx_post='/b', color='green', title='B', "
            "hx_target='closest tr', hx_swap='outerHTML', timeout=500, after='reload') }}"
        ),
        "button",
    )

    for name in ("x-data", "@click", ":class", ":title"):
        assert first[name] == second[name]


def test_the_confirm_button_carries_what_the_request_needs() -> None:
    button = _one(
        _macros(
            "{{ confirm_action_btn(hx_delete='/a', hx_target='closest tr', "
            "hx_swap='outerHTML', color='amber', timeout=500, after='reload') }}"
        ),
        "button",
    )

    assert button["data-ajax-method"] == "DELETE"
    assert button["data-ajax-url"] == "/a"
    assert button["data-ajax-target"] == "closest tr"
    assert button["data-ajax-swap"] == "outerHTML"
    assert button["data-ajax-after"] == "reload"
    assert button["data-confirm-timeout"] == "500"
    assert "bg-amber-500" in (button["data-armed-class"] or "")
    assert "bg-amber-600/20" in (button["data-normal-class"] or "")


def test_a_dashboard_delete_names_its_source_and_page_version_as_data() -> None:
    button = _one(
        _macros(
            "{{ delete_icon_button('/a', 'row', hx_target='#dashboard-items', "
            "ajax_source='#dashboard-items', "
            "page_version_from='#dashboard-page-version') }}"
        ),
        "button",
    )

    assert button["data-ajax-source"] == "#dashboard-items"
    assert button["data-page-version-from"] == "#dashboard-page-version"


def test_no_macro_takes_javascript_for_the_confirm_button() -> None:
    with pytest.raises(TypeError):
        _macros("{{ confirm_action_btn(hx_post='/x', ajax_extra='alert(1)') }}")


def test_the_panel_closes_through_one_method_on_its_wrapper() -> None:
    html = _macros("{% call panel_wrapper() %}{{ panel_header('Title') }}{% endcall %}")

    wrapper = _find(html, "div")[0]
    assert "close()" in (wrapper["x-data"] or "")
    assert "this.show = false" in (wrapper["x-data"] or "")
    assert "getElementById('panel-container')" in (wrapper["x-data"] or "")
    assert wrapper["@keydown.escape.window"] == "close()"
    assert wrapper["@close-side-panel.window"] == "close()"
    assert wrapper["@click.self"] == "close()"
    backdrop = next(d for d in _find(html, "div") if "@click" in d)
    assert backdrop["@click"] == "close()"
    close_button = _one(html, "button")
    assert close_button["@click"] == "close()"
    assert "data-close-panel" in close_button


@pytest.mark.parametrize("value", HOSTILE)
def test_a_finished_jobs_values_reach_its_event_as_data(value: str) -> None:
    job = SimpleNamespace(
        id=value, kind=value, target_id=value, status="done", progress=None, error=None
    )
    html = _partial(
        "partials/maintenance/job_status.html",
        job=job,
        job_status_url="/status",
    )

    element = _find(html, "div")[0]
    assert element["data-job-id"] == value
    assert element["data-job-kind"] == value
    assert element["data-job-target"] == value
    handler = element["hx-on::load"] or ""
    assert "alert" not in handler
    assert "this.dataset.jobId" in handler
    assert "this.dataset.jobKind" in handler
    assert "this.dataset.jobTarget" in handler
    assert not [k for k in element if k.startswith("on")]


def test_a_running_job_has_no_finished_event() -> None:
    job = SimpleNamespace(
        id="1", kind="k", target_id=None, status="running", progress=None, error=None
    )
    html = _partial(
        "partials/maintenance/job_status.html",
        job=job,
        job_status_url="/status",
    )

    assert "hx-on::load" not in _find(html, "div")[0]


def _network_scan_context(hostname: str) -> dict[str, Any]:
    host = {
        "ip": "10.0.0.1",
        "hostname": hostname,
        "responds": True,
        "open_ports": [80, 22, 3306],
        "all_checks_exist": False,
    }
    result = {
        "hosts_scanned": 1,
        "hosts_responding": 1,
        "scan_duration_seconds": 1,
        "summary": None,
        "discovered_hosts": [host],
        "categorized_hosts": {"web": [host], "ssh": [host], "database": [host]},
    }
    return {
        "job": SimpleNamespace(result=result, agent_id="a", duration_seconds=1),
        "job_result": panel_result("network_scan", result),
        "scan_response_pct": 100,
        "scan_hosts": [host],
        "scan_new_count": 1,
        "scan_monitored_count": 0,
        "scan_chunks_with_findings": [],
        "scan_chunks_without_findings": [],
        "existing_checks": set(),
        "available_check_tags": [hostname],
        "agents": [],
        "check_defaults": SimpleNamespace(
            interval_seconds=60,
            timeout_seconds=10,
            retry_attempts=1,
            expected_status=200,
            verify_ssl=False,
        ),
        "scan_action_urls": SimpleNamespace(
            quick_create="/quick",
            bulk_ping="/ping",
            bulk_web="/web",
            bulk_ssh="/ssh",
            bulk_database="/db",
        ),
    }


@pytest.mark.parametrize("value", HOSTILE)
def test_a_scanned_hostname_stays_inside_its_quick_create_values(value: str) -> None:
    html = _partial(
        "partials/jobs/network_scan_detail.html",
        **_network_scan_context(value),
    )

    buttons = [b for b in _find(html, "button") if b.get("hx-vals")]
    assert buttons
    for button in buttons:
        assert not [k for k in button if k.startswith("on")]
        assert value in json.loads(button["hx-vals"] or "")["target"]
    pills = [b for b in _find(html, "button") if "data-tag" in b]
    assert [p["data-tag"] for p in pills] == [value]


_BUTTONS = '{% from "macros/buttons.html" import btn_disable, text_btn %}'


def _render(source: str, **context: Any) -> str:
    return templates.env.from_string(_BUTTONS + source).render(**context)


def test_a_missing_field_under_attrs_raises_at_render() -> None:
    """A renamed URL is a loud error, never a button that posts nowhere."""
    alert = SimpleNamespace(urls=SimpleNamespace())

    with pytest.raises(UndefinedError):
        _render("{{ btn_disable(attrs={'hx-post': alert.urls.toggle}) }}", alert=alert)


def test_a_missing_name_under_attrs_raises_at_render() -> None:
    with pytest.raises(UndefinedError):
        _render("{{ btn_disable(attrs={'data-tag': no_such_name}) }}")


@pytest.mark.parametrize(
    "source",
    [
        """{% set v %}x" onclick="1{% endset %}{{ btn_disable(attrs={'data-a': v}) }}""",
        "{{ btn_disable(attrs={'data-a': name | e}) }}",
        "{{ btn_disable(attrs={'data-a': name | safe}) }}",
    ],
)
def test_markup_under_attrs_is_refused(source: str) -> None:
    with pytest.raises(ValueError, match="plain value"):
        _render(source, name="a&b")


class _StrIsMarkup:
    def __str__(self) -> str:
        return Markup('x" onclick="1')


def test_a_value_whose_str_is_markup_stays_in_its_attribute() -> None:
    parser = _Tags()
    parser.feed(_render("{{ btn_disable(attrs={'data-a': v}) }}", v=_StrIsMarkup()))

    button = dict(parser.tags[0][1])
    assert "onclick" not in button
    assert button["data-a"] == 'x" onclick="1'


def test_none_leaves_the_attribute_out_and_other_values_keep_it() -> None:
    out = _render(
        """{{ btn_disable(attrs={'data-none': none, 'data-empty': '', 'data-zero': 0}) }}"""
    )

    assert "data-none" not in out
    assert 'data-empty=""' in out
    assert 'data-zero="0"' in out


@pytest.mark.parametrize(
    "name",
    [
        "bad name",
        "a>b",
        'a"b',
        "a=b",
        "a/b",
        "",
        "a\x00b",
        "a\x01b",
        "a\x7fb",
        "a`b",
        "a&b",
    ],
)
def test_a_key_that_is_not_an_attribute_name_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="attribute name"):
        _render("{{ btn_disable(attrs={name: 'x'}) }}", name=name)
