# -*- coding: utf-8 -*-
"""The six linux_ui_* MCP tools: published with all four hints (tree / screenshot read-only,
the rest destructive) and forwarding their arguments to labhand.linux_ui unchanged."""
import asyncio

import pytest

pytest.importorskip("mcp")

from labhand import linux_ui, mcp_server  # noqa: E402
from labhand._result import VMResult  # noqa: E402

TOOLS = ("linux_ui_start", "linux_ui_tree", "linux_ui_click", "linux_ui_type",
         "linux_ui_screenshot", "linux_ui_stop")


def _ann(name):
    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    a = tools[name].annotations
    get = lambda c: getattr(a, c) if hasattr(a, c) else getattr(  # noqa: E731
        a, {"readOnlyHint": "read_only_hint", "destructiveHint": "destructive_hint",
            "idempotentHint": "idempotent_hint", "openWorldHint": "open_world_hint"}[c])
    return tuple(get(h) for h in ("readOnlyHint", "destructiveHint", "idempotentHint",
                                  "openWorldHint")), a.title


@pytest.mark.parametrize("name", TOOLS)
def test_linux_ui_mcp_hints(name):
    hints, title = _ann(name)
    assert title and all(isinstance(h, bool) for h in hints)
    if name in ("linux_ui_tree", "linux_ui_screenshot"):
        assert hints == (True, False, True, False)
    else:
        assert hints[0] is False and hints[1] is True
    if name == "linux_ui_stop":
        assert hints == (False, True, True, False)


def test_linux_ui_mcp_forwards_arguments(monkeypatch):
    seen = {}
    for n in TOOLS:
        monkeypatch.setattr(linux_ui, n, (lambda n: lambda *a, **k: seen.setdefault(n, (a, k))
                                          and VMResult(True, n))(n))
    assert mcp_server.linux_ui_start("mousepad", max_minutes=5) == "linux_ui_start"
    assert seen["linux_ui_start"] == (("mousepad",), {
        "name": None, "screen": "1280x800x24", "wait": 5, "host": None, "timeout": 60,
        "max_minutes": 5})
    mcp_server.linux_ui_tree("s-1", match="Save")
    assert seen["linux_ui_tree"] == (("s-1",), {"match": "Save", "limit": 250, "depth": 30,
                                                "showing_only": True, "host": None,
                                                "timeout": 45})
    mcp_server.linux_ui_click("s-1", "#3", action="press")
    assert seen["linux_ui_click"] == (("s-1", "#3"), {"action": "press", "host": None,
                                                      "timeout": 45})
    mcp_server.linux_ui_type("s-1", "hi", target="text:", enter=True)
    assert seen["linux_ui_type"] == (("s-1", "hi"), {"target": "text:", "enter": True,
                                                     "host": None, "timeout": 45})
    mcp_server.linux_ui_screenshot("s-1")
    assert seen["linux_ui_screenshot"] == (("s-1",), {"host": None, "timeout": 30})
    mcp_server.linux_ui_stop()
    assert seen["linux_ui_stop"] == ((), {"session": None, "host": None, "timeout": 30})
