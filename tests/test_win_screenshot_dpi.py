# -*- coding: utf-8 -*-
"""win_screenshot captures the WHOLE desktop on a scaled display.

MEASURED 2026-11-21 (Windows 11 ARM64 at 200 % scaling, physical 2640x1650): a DPI-unaware
PowerShell reads SystemInformation.VirtualScreen in logical pixels (1320x825), and
CopyFromScreen then copies only that top-left quarter of the physical screen.
win_gui_run looked fine only because its body loads UI Automation first, which happens to
make the process DPI-aware. The screenshot body must declare DPI awareness itself, before
it reads the screen bounds. VM-free: the session-1 body is captured, nothing is run.
"""
import re

from labhand import windows


def _capture_bodies(monkeypatch):
    bodies = []

    def fake_session1(host, ps_body, **kw):
        bodies.append(ps_body)
        m = re.search(r"(_lh_screen_[A-Za-z0-9_]+\.png)", ps_body)
        return f"SCREEN 2640x1650 -> {m.group(1) if m else 'x.png'}", False

    monkeypatch.setattr(windows, "_session1", fake_session1)
    monkeypatch.setattr(windows, "_win_shot_image",
                        lambda label, host, shot: ({"media_type": "image/png", "data": ""},
                                                   f"screenshot of {label}"))
    monkeypatch.setattr(windows, "_sync_to_windows", lambda p, h: (True, ""))
    monkeypatch.setattr(windows.remote, "remove_quiet", lambda names, host=None: None)
    return bodies


def _dpi_before_bounds(body):
    dpi = body.find("SetProcessDPIAware()")
    bounds = body.find("SystemInformation]::VirtualScreen")
    assert bounds >= 0, "the screenshot body no longer reads VirtualScreen?"
    return 0 <= dpi < bounds and "[LhDpi.User32]::SetProcessDPIAware()" in body


def test_screenshot_body_declares_dpi_awareness_before_reading_bounds():
    assert _dpi_before_bounds(windows._ps_screenshot("_lh_screen_1_2_abcdef01.png"))


def test_win_screenshot_sends_a_dpi_aware_body(monkeypatch):
    bodies = _capture_bodies(monkeypatch)
    r = windows.win_screenshot(host="windows")
    assert r.ok, r.summary
    assert len(bodies) == 1 and _dpi_before_bounds(bodies[0])


def test_win_gui_run_screenshot_is_dpi_aware_and_type_defined_once(monkeypatch):
    bodies = _capture_bodies(monkeypatch)
    windows.win_gui_run(r"C:\Windows\System32\notepad.exe", kill=False, host="windows")
    assert len(bodies) == 1 and _dpi_before_bounds(bodies[0])
    # the Add-Type is guarded, so a body carrying the snippet twice cannot fail on
    # "type already exists"
    assert 'if (-not ("LhDpi.User32" -as [type]))' in bodies[0]
