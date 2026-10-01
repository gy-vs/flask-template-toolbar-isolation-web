from __future__ import annotations

import json
import sys
import typing as t
import uuid
from collections import deque
from weakref import WeakKeyDictionary

from flask import abort
from flask import current_app
from flask import Flask
from flask import g
from flask import request
from flask import Response
from flask import template_rendered
from flask import url_for
from jinja2 import Template

from .. import module
from . import DebugPanel


class TemplateDebugPanel(DebugPanel):
    """Panel that displays the time a response took in milliseconds."""

    name = "Template"
    has_content = True

    # Save the context for the 5 most recent requests. Each application
    # served by this process gets its own cache, so that one application
    # can never see or edit another application's templates.
    template_cache: WeakKeyDictionary[
        Flask, deque[tuple[str, list[dict[str, t.Any]]]]
    ] = WeakKeyDictionary()

    @classmethod
    def get_cache(cls) -> deque[tuple[str, list[dict[str, t.Any]]]]:
        """Return the template cache for the current application."""
        app = current_app._get_current_object()  # type: ignore[attr-defined]

        if app not in cls.template_cache:
            cls.template_cache[app] = deque(maxlen=5)

        return cls.template_cache[app]

    @classmethod
    def get_cache_for_key(cls, key: str) -> list[dict[str, t.Any]]:
        for cache_key, value in cls.get_cache():
            if key == cache_key:
                return value

        raise KeyError(key)

    def __init__(self, *args: t.Any, **kwargs: t.Any) -> None:
        super().__init__(*args, **kwargs)
        self.key: str = str(uuid.uuid4())
        self.templates: list[dict[str, t.Any]] = []
        # Only record templates rendered by the application handling this
        # request. Other applications may be served by the same process.
        template_rendered.connect(
            self._store_template_info,
            sender=current_app._get_current_object(),  # type: ignore[attr-defined]
        )

    def _store_template_info(self, sender: t.Any, **kwargs: t.Any) -> None:
        # only record in the cache if the editor is enabled and there is
        # actually a template for this request
        if not self.templates and is_editor_enabled():
            self.get_cache().append((self.key, self.templates))

        self.templates.append(kwargs)

    def nav_title(self) -> str:
        return "Templates"

    def nav_subtitle(self) -> str:
        return f"{len(self.templates)} rendered"

    def title(self) -> str:
        return "Templates"

    def url(self) -> str:
        return ""

    def content(self) -> str:
        return self.render(
            "panels/template.html",
            {
                "key": self.key,
                "templates": self.templates,
                "editable": is_editor_enabled(),
            },
        )


def is_editor_enabled() -> bool:
    return current_app.config.get("DEBUG_TB_TEMPLATE_EDITOR_ENABLED", False)  # type: ignore


def require_enabled() -> None:
    if not is_editor_enabled():
        abort(403)


def _get_source(template: Template) -> str:
    if template.filename is None:
        return ""

    with open(template.filename, "rb") as fp:
        source = fp.read()

    return source.decode(_template_encoding())


def _template_encoding() -> str:
    return getattr(current_app.jinja_loader, "encoding", "utf-8")


def _get_templates_or_404(key: str) -> list[dict[str, t.Any]]:
    """Return the cached template info for ``key``.

    The cache is scoped to the current application, so a key issued by
    another application, or one whose entry has expired, is not found.
    """
    try:
        return TemplateDebugPanel.get_cache_for_key(key)
    except KeyError:
        abort(404)


def _unknown_key_response() -> Response:
    """JSON error returned by the editor AJAX endpoints when ``key`` is not
    found in the current application's cache, for example because the
    recorded request expired or the key belongs to another application.
    """
    return Response(
        json.dumps(
            {
                "error": (
                    "The recorded template data for this editing session is"
                    " no longer available. Preview and save are disabled;"
                    " reload the page and reopen the editor."
                )
            }
        ),
        status=404,
        mimetype="application/json",
    )


@module.route("/template/<key>")
def template_editor(key: str) -> str:
    require_enabled()
    # TODO set up special loader that caches templates it loads
    # and can override template contents
    templates = [t["template"] for t in _get_templates_or_404(key)]
    return g.debug_toolbar.render(  # type: ignore[no-any-return]
        "panels/template_editor.html",
        {
            "static_path": url_for("_debug_toolbar.static", filename=""),
            "request": request,
            "templates": [
                {"name": t.name, "source": _get_source(t)} for t in templates
            ],
        },
    )


@module.route("/template/<key>/save", methods=["POST"])
def save_template(key: str) -> str | Response:
    require_enabled()

    try:
        template = TemplateDebugPanel.get_cache_for_key(key)[0]["template"]
    except KeyError:
        return _unknown_key_response()

    content = request.form["content"].encode(_template_encoding())

    with open(template.filename, "wb") as fp:
        fp.write(content)

    return "ok"


@module.route("/template/<key>", methods=["POST"])
def template_preview(key: str) -> str | Response:
    require_enabled()

    try:
        context = TemplateDebugPanel.get_cache_for_key(key)[0]["context"]
    except KeyError:
        return _unknown_key_response()

    content = request.form["content"]
    env = current_app.jinja_env.overlay(autoescape=True)

    try:
        template = env.from_string(content)
        return template.render(context)
    except Exception as e:
        tb = sys.exc_info()[2]

        try:
            while tb.tb_next:  # type: ignore[union-attr]
                tb = tb.tb_next  # type: ignore[union-attr]

            msg = {"lineno": tb.tb_lineno, "error": str(e)}  # type: ignore[union-attr]
            return Response(json.dumps(msg), status=400, mimetype="application/json")
        finally:
            del tb
