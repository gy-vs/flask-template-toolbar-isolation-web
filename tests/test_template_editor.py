from __future__ import annotations

import json
import re
import threading
import typing as t
from pathlib import Path

import pytest
from flask import Flask
from flask import render_template

from flask_debugtoolbar import DebugToolbarExtension


def make_app(name: str, template_folder: Path) -> Flask:
    app = Flask(name, template_folder=str(template_folder))
    app.config["DEBUG"] = True
    app.config["SECRET_KEY"] = f"secret-{name}"
    app.config["DEBUG_TB_TEMPLATE_EDITOR_ENABLED"] = True
    DebugToolbarExtension(app)
    return app


@pytest.fixture
def two_apps(tmp_path: Path) -> t.Iterator[tuple[Flask, Flask, Path, Path]]:
    """Two independent applications with separate template folders, served
    by the same Python process."""
    dir_a = tmp_path / "a_templates"
    dir_b = tmp_path / "b_templates"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "a.html").write_text(
        "<html><body><h1>App A</h1><p>{{ a_value }}</p></body></html>"
    )
    (dir_b / "b.html").write_text(
        "<html><body><h1>App B</h1><p>{{ b_value }}</p></body></html>"
    )

    app_a = make_app("test_app_a", dir_a)
    app_b = make_app("test_app_b", dir_b)

    @app_a.route("/")
    def a_index() -> str:
        return render_template("a.html", a_value="A-CONTEXT-VALUE")

    @app_b.route("/")
    def b_index() -> str:
        return render_template("b.html", b_value="B-CONTEXT-VALUE")

    yield app_a, app_b, dir_a, dir_b


def extract_editor_key(html: str) -> str:
    match = re.search(r"/_debug_toolbar/views/template/([0-9a-f-]+)", html)
    assert match, "editor link not found in toolbar"
    return match.group(1)


def test_panel_only_shows_templates_from_own_app(
    two_apps: tuple[Flask, Flask, Path, Path],
) -> None:
    """While app A's request is paused, app B renders its own templates.
    A's toolbar must only contain A's templates and context."""
    app_a, app_b, _, _ = two_apps
    a_entered = threading.Event()
    b_done = threading.Event()

    @app_a.route("/slow")
    def a_slow() -> str:
        a_entered.set()
        assert b_done.wait(10)
        return render_template("a.html", a_value="A-CONTEXT-VALUE")

    client_a = app_a.test_client()
    result: dict[str, str] = {}

    def get_a() -> None:
        result["html"] = client_a.get("/slow").get_data(as_text=True)

    thread = threading.Thread(target=get_a)
    thread.start()
    assert a_entered.wait(10)
    b_html = app_b.test_client().get("/").get_data(as_text=True)
    b_done.set()
    thread.join(10)

    a_html = result["html"]
    # A's panel shows its own template and context...
    assert "a.html" in a_html
    assert "A-CONTEXT-VALUE" in a_html
    # ...but nothing rendered by B's concurrent request.
    assert "b.html" not in a_html
    assert "B-CONTEXT-VALUE" not in a_html
    # B's own panel is unaffected.
    assert "b.html" in b_html
    assert "B-CONTEXT-VALUE" in b_html


def test_editor_routes_reject_key_from_other_app(
    two_apps: tuple[Flask, Flask, Path, Path],
) -> None:
    """A key issued by app B must not be usable under app A's toolbar
    routes, for the editor, the preview, or the save endpoint."""
    app_a, app_b, _, dir_b = two_apps
    client_a = app_a.test_client()
    b_html = app_b.test_client().get("/").get_data(as_text=True)
    b_key = extract_editor_key(b_html)

    response = client_a.get(f"/_debug_toolbar/views/template/{b_key}")
    assert response.status_code == 404
    assert "App B" not in response.get_data(as_text=True)

    response = client_a.post(
        f"/_debug_toolbar/views/template/{b_key}", data={"content": "{{ b_value }}"}
    )
    assert response.status_code == 404
    assert "B-CONTEXT-VALUE" not in response.get_data(as_text=True)

    before = (dir_b / "b.html").read_text()
    response = client_a.post(
        f"/_debug_toolbar/views/template/{b_key}/save", data={"content": "OVERWRITTEN"}
    )
    assert response.status_code == 404
    assert (dir_b / "b.html").read_text() == before


def test_editor_routes_work_for_own_app(
    two_apps: tuple[Flask, Flask, Path, Path],
) -> None:
    """The normal in-app editing flow is unchanged: the editor shows the
    template source, the preview renders with the recorded context, and
    saving writes the template file."""
    app_a, _, dir_a, _ = two_apps
    client = app_a.test_client()
    key = extract_editor_key(client.get("/").get_data(as_text=True))

    response = client.get(f"/_debug_toolbar/views/template/{key}")
    assert response.status_code == 200
    assert "App A" in response.get_data(as_text=True)

    response = client.post(
        f"/_debug_toolbar/views/template/{key}",
        data={"content": "value={{ a_value }}"},
    )
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "value=A-CONTEXT-VALUE"

    response = client.post(
        f"/_debug_toolbar/views/template/{key}/save",
        data={"content": "<html><body>updated</body></html>"},
    )
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "ok"
    assert (dir_a / "a.html").read_text() == "<html><body>updated</body></html>"


def test_preview_syntax_error_still_reports_line(
    two_apps: tuple[Flask, Flask, Path, Path],
) -> None:
    app_a, _, _, _ = two_apps
    client = app_a.test_client()
    key = extract_editor_key(client.get("/").get_data(as_text=True))

    response = client.post(
        f"/_debug_toolbar/views/template/{key}", data={"content": "{% if %}"}
    )
    assert response.status_code == 400
    assert response.mimetype == "application/json"
    data = json.loads(response.get_data(as_text=True))
    assert data["lineno"] == 1
    assert data["error"]


def test_expired_editor_key_reports_json_error(
    two_apps: tuple[Flask, Flask, Path, Path],
) -> None:
    """Once the recorded request has expired from the cache, the editor
    endpoints answer with a JSON 404 instead of an HTML error page, and a
    stale save must not touch the template file."""
    app_a, _, dir_a, _ = two_apps
    client = app_a.test_client()
    key = extract_editor_key(client.get("/").get_data(as_text=True))

    # the editor page still opens while the key is valid
    assert client.get(f"/_debug_toolbar/views/template/{key}").status_code == 200

    # expire the key: the cache only keeps the 5 most recent requests
    for _ in range(5):
        client.get("/")

    response = client.get(f"/_debug_toolbar/views/template/{key}")
    assert response.status_code == 404

    response = client.post(
        f"/_debug_toolbar/views/template/{key}", data={"content": "hello"}
    )
    assert response.status_code == 404
    assert response.mimetype == "application/json"
    assert json.loads(response.get_data(as_text=True))["error"]

    before = (dir_a / "a.html").read_text()
    response = client.post(
        f"/_debug_toolbar/views/template/{key}/save", data={"content": "OVERWRITTEN"}
    )
    assert response.status_code == 404
    assert response.mimetype == "application/json"
    assert (dir_a / "a.html").read_text() == before


def test_panel_shows_multiple_templates_of_one_request(
    two_apps: tuple[Flask, Flask, Path, Path],
) -> None:
    app_a, _, dir_a, _ = two_apps
    (dir_a / "a_extra.html").write_text("<p>extra {{ a_value }}</p>")

    @app_a.route("/multi")
    def a_multi() -> str:
        return render_template("a.html", a_value="A-CONTEXT-VALUE") + render_template(
            "a_extra.html", a_value="A-CONTEXT-VALUE"
        )

    html = app_a.test_client().get("/multi").get_data(as_text=True)
    assert "a.html" in html
    assert "a_extra.html" in html
