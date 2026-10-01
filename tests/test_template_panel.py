from __future__ import annotations

import re
import threading
import typing as t
from pathlib import Path

import pytest
from flask import Flask
from flask import render_template
from werkzeug.test import TestResponse

from flask_debugtoolbar import DebugToolbarExtension
from flask_debugtoolbar.panels.template import TemplateDebugPanel

EDITOR_PATH = "/_debug_toolbar/views/template"


def create_app(tmp_path: Path, name: str, templates: dict[str, str]) -> Flask:
    template_dir = tmp_path / name
    template_dir.mkdir()

    for template_name, source in templates.items():
        (template_dir / template_name).write_text(source)

    app = Flask(name, template_folder=str(template_dir))
    app.config["DEBUG"] = True
    app.config["SECRET_KEY"] = f"secret-{name}"
    app.config["DEBUG_TB_TEMPLATE_EDITOR_ENABLED"] = True
    # NOTE: keep the default DEBUG_TB_PANELS. Restricting the list would
    # postpone importing the other panel modules until after this app has
    # registered the shared toolbar blueprint, at which point their routes
    # can no longer be added to it.
    DebugToolbarExtension(app)
    return app


def simple_app(tmp_path: Path, name: str) -> Flask:
    app = create_app(
        tmp_path,
        name,
        {
            f"{name}_page.html": (
                f"<html><body>{name} {{{{ {name}_value }}}}</body></html>"
            )
        },
    )

    @app.route("/")
    def index() -> str:
        return render_template(
            f"{name}_page.html", **{f"{name}_value": f"{name}-VALUE"}
        )

    return app


def get_editor_key(response: TestResponse) -> str:
    match = re.search(rf"{EDITOR_PATH}/([0-9a-f-]+)", response.data.decode("utf-8"))
    assert match is not None, "toolbar did not offer a template editor link"
    return match.group(1)


@pytest.fixture(autouse=True)
def clear_template_cache() -> t.Iterator[None]:
    TemplateDebugPanel.template_cache.clear()
    yield
    TemplateDebugPanel.template_cache.clear()


def test_panel_only_records_own_app_during_overlapping_requests(
    tmp_path: Path,
) -> None:
    """While app A's request is paused mid-view, app B handles a request.
    A's toolbar must only list A's templates, B's only B's."""
    app_a = simple_app(tmp_path, "app_a")
    app_b = simple_app(tmp_path, "app_b")

    a_waiting = threading.Event()
    b_finished = threading.Event()

    @app_a.route("/slow")
    def a_slow() -> str:
        a_waiting.set()
        assert b_finished.wait(timeout=10)
        return render_template("app_a_page.html", app_a_value="app_a-VALUE")

    holder: dict[str, TestResponse] = {}
    errors: list[Exception] = []

    def request_a() -> None:
        try:
            holder["response"] = app_a.test_client().get("/slow")
        except Exception as e:  # pragma: no cover - failure reporting
            errors.append(e)

    thread = threading.Thread(target=request_a)
    thread.start()
    try:
        assert a_waiting.wait(timeout=10)
        response_b = app_b.test_client().get("/")
    finally:
        b_finished.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert not errors

    html_a = holder["response"].data.decode("utf-8")
    assert "app_a_page.html" in html_a
    assert "app_a-VALUE" in html_a
    assert "app_b_page.html" not in html_a
    assert "app_b-VALUE" not in html_a

    html_b = response_b.data.decode("utf-8")
    assert "app_b_page.html" in html_b
    assert "app_b-VALUE" in html_b
    assert "app_a_page.html" not in html_b


def test_editor_key_of_other_app_is_not_usable(tmp_path: Path) -> None:
    """An editor URL issued by app B must not read or modify B's template
    when used under app A's toolbar routes."""
    app_a = simple_app(tmp_path, "app_a")
    app_b = simple_app(tmp_path, "app_b")
    b_template = tmp_path / "app_b" / "app_b_page.html"

    key_b = get_editor_key(app_b.test_client().get("/"))
    client_a = app_a.test_client()

    response = client_a.get(f"{EDITOR_PATH}/{key_b}")
    assert response.status_code == 404
    assert "app_b_page.html" not in response.data.decode("utf-8")

    response = client_a.post(
        f"{EDITOR_PATH}/{key_b}", data={"content": "{{ app_b_value }}"}
    )
    assert response.status_code == 404
    assert response.mimetype == "application/json"
    assert "app_b-VALUE" not in response.data.decode("utf-8")

    before = b_template.read_text()
    response = client_a.post(f"{EDITOR_PATH}/{key_b}/save", data={"content": "X"})
    assert response.status_code == 404
    assert response.mimetype == "application/json"
    assert b_template.read_text() == before


def test_editor_preview_and_save_for_own_app(tmp_path: Path) -> None:
    """The regular same-app editing flow keeps working."""
    app = simple_app(tmp_path, "app_a")
    template = tmp_path / "app_a" / "app_a_page.html"
    client = app.test_client()

    key = get_editor_key(client.get("/"))

    response = client.get(f"{EDITOR_PATH}/{key}")
    assert response.status_code == 200
    assert "app_a_page.html" in response.data.decode("utf-8")

    response = client.post(
        f"{EDITOR_PATH}/{key}", data={"content": "{{ app_a_value }}"}
    )
    assert response.status_code == 200
    assert "app_a-VALUE" in response.data.decode("utf-8")

    response = client.post(
        f"{EDITOR_PATH}/{key}/save",
        data={"content": "<html><body>updated {{ app_a_value }}</body></html>"},
    )
    assert response.status_code == 200
    assert "updated" in template.read_text()


def test_expired_editor_key_is_a_clean_error(tmp_path: Path) -> None:
    """Once the request's cache entry has been evicted, the editor, preview
    and save endpoints answer 404 (JSON for the XHR endpoints) and the
    template file is left alone."""
    app = simple_app(tmp_path, "app_a")
    template = tmp_path / "app_a" / "app_a_page.html"
    client = app.test_client()

    key = get_editor_key(client.get("/"))

    # the cache keeps the 5 most recent requests, so this evicts the key
    for _ in range(TemplateDebugPanel.template_cache.maxlen):  # type: ignore[arg-type]
        client.get("/")

    assert client.get(f"{EDITOR_PATH}/{key}").status_code == 404

    response = client.post(f"{EDITOR_PATH}/{key}", data={"content": "x"})
    assert response.status_code == 404
    assert response.mimetype == "application/json"

    before = template.read_text()
    response = client.post(f"{EDITOR_PATH}/{key}/save", data={"content": "X"})
    assert response.status_code == 404
    assert response.mimetype == "application/json"
    assert template.read_text() == before


def test_panel_lists_all_templates_rendered_in_a_request(tmp_path: Path) -> None:
    """A request rendering several templates still shows each of them."""
    app = create_app(
        tmp_path,
        "app_a",
        {
            "page.html": "<html><body>page</body></html>",
            "partial.html": "<html><body>partial</body></html>",
        },
    )

    @app.route("/")
    def index() -> str:
        render_template("partial.html")
        return render_template("page.html")

    html = app.test_client().get("/").data.decode("utf-8")
    assert "page.html" in html
    assert "partial.html" in html
    assert "2 rendered" in html
