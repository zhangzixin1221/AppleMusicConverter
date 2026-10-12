"""Layout tests for the Tk interface.

These drive the real window: build it exactly as a user would see it, then check
that (a) nothing is clipped at the size it opens with, (b) the expanding areas
grow and shrink with the window, and (c) widgets with a natural size stay that
size instead of being stretched out of proportion.

The window is built exactly once for the whole module, on purpose. Creating a
throwaway Tk root just to probe availability would set up the process as
DPI-unaware before ``run_gui`` gets to declare otherwise, and the measurements
would then be taken at the wrong scale. Skipped when there is no display at all.
"""

from __future__ import annotations

import time
import tkinter

import pytest

from amc import gui

ALLOW = 2      # pixel slack for rounding


def _walk(widget):
    for child in widget.winfo_children():
        yield child
        yield from _walk(child)


def _find(widget, klass: str, text: str | None = None):
    for child in _walk(widget):
        if child.winfo_class() != klass:
            continue
        if text is None:
            return child
        try:
            if text in str(child.cget("text")):
                return child
        except tkinter.TclError:
            continue
    return None


def _clipped(parent) -> list[tuple]:
    """Children that stick out of their parent, i.e. that would be cut off."""
    bad = []
    pw, ph = parent.winfo_width(), parent.winfo_height()
    for child in parent.winfo_children():
        if not child.winfo_ismapped():
            continue
        x, y = child.winfo_x(), child.winfo_y()
        w, h = child.winfo_width(), child.winfo_height()
        if w <= 1 or h <= 1:
            continue
        if (x < -ALLOW or y < -ALLOW
                or x + w > pw + ALLOW or y + h > ph + ALLOW):
            bad.append({"class": child.winfo_class(), "x": x, "y": y,
                        "w": w, "h": h, "parent": (pw, ph)})
    return bad


def _clipped_deep(parent) -> list[tuple]:
    out = list(_clipped(parent))
    for child in parent.winfo_children():
        out += _clipped_deep(child)
    return out


# Controls whose whole point is the text they draw: if one of these is narrower
# than it asked for, its label is being cut off.
TEXT_CONTROLS = {"TLabel", "TButton", "TCheckbutton", "TEntry", "TSpinbox",
                 "TLabelframe"}


def _squeezed(root) -> list[tuple]:
    bad = []
    for child in _walk(root):
        if child.winfo_class() not in TEXT_CONTROLS or not child.winfo_ismapped():
            continue
        missing = child.winfo_reqwidth() - child.winfo_width()
        if missing > ALLOW:
            bad.append({"class": child.winfo_class(),
                        "text": (str(child.cget("text"))[:20]
                                 if child.winfo_class() != "TEntry" else ""),
                        "width": child.winfo_width(),
                        "wanted": child.winfo_reqwidth()})
    return bad


class Session:
    """Layout recorded while the real interface was built and resized."""

    def __init__(self):
        self.code = None
        self.initial: dict = {}
        self.sizes: dict[str, dict] = {}
        self.can_grow = False


def _snapshot(window, outer) -> dict:
    def box(text: str) -> tuple[tuple[int, int], tuple[int, int]]:
        widget = _find(window, "TButton", text)
        return ((widget.winfo_width(), widget.winfo_height()),
                (widget.winfo_reqwidth(), widget.winfo_reqheight()))

    files_frame = _find(window, "TLabelframe", "待转换文件")
    log_frame = _find(window, "TLabelframe", "日志")
    hint = _find(window, "TLabel", "输出文件夹留空")
    return {
        "client": (window.winfo_width(), window.winfo_height()),
        "clipped": _clipped_deep(outer),
        "squeezed": _squeezed(window),
        "list": (files_frame.winfo_width(), files_frame.winfo_height()),
        "log": (log_frame.winfo_width(), log_frame.winfo_height()),
        "button": box("清空"),
        "start_button": box("开始转换"),
        "wrap": int(hint.cget("wraplength")),
    }


@pytest.fixture(scope="module")
def session():
    monkeypatch = pytest.MonkeyPatch()
    result = Session()

    def scripted_mainloop(self, n=0):
        try:
            self.update_idletasks()
            self.update()
            time.sleep(0.15)          # let the 120ms pump tick at least once
            self.update()

            outer = self.winfo_children()[0]
            result.initial = _snapshot(self, outer)
            result.initial.update({
                "geometry": self.geometry(),
                "required": (self.winfo_reqwidth(), self.winfo_reqheight()),
                "screen": (self.winfo_screenwidth(), self.winfo_screenheight()),
                "minsize": self.minsize(),
                "has_hscroll": _find(self, "TScrollbar") is not None,
            })

            client_w, client_h = self.winfo_width(), self.winfo_height()
            screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
            min_w, min_h = self.minsize()
            # Sizes relative to what this display actually allowed, so the test
            # means the same thing on a 1x laptop and a 200%-scaled 4K monitor.
            big_w = min(client_w + 260, screen_w - 80)
            big_h = min(client_h + 180, screen_h - 110)
            result.can_grow = big_w > client_w + 40 and big_h > client_h + 40
            # minimum: the smallest window the user may drag to, which also wraps
            # the guidance text the hardest. large: room to spare.
            # short: stretched wide without any extra height.
            cases = {"minimum": (min_w, min_h), "large": (big_w, big_h),
                     "short": (big_w, min_h)}

            for label, (case_w, case_h) in cases.items():
                self.geometry(f"{case_w}x{case_h}")
                self.update_idletasks()
                self.update()
                result.sizes[label] = _snapshot(self, outer)
        finally:
            self.destroy()

    monkeypatch.setattr(tkinter.Tk, "mainloop", scripted_mainloop)
    try:
        result.code = gui.run_gui([])
    except tkinter.TclError as exc:            # no display / no Tk libraries
        pytest.skip(f"没有可用的图形环境: {exc}")
    finally:
        monkeypatch.undo()
    if not result.initial:
        # run_gui() exits early when tkinter itself is missing.
        pytest.skip("图形界面没有启动（缺少 tkinter）")
    return result


# --------------------------------------------------------------------------
# the window opens showing everything
# --------------------------------------------------------------------------

def test_window_opens_with_every_widget_visible(session):
    assert session.code == 0
    assert session.initial["clipped"] == [], (
        f"开窗时被裁掉的组件: {session.initial['clipped']}")


def test_every_label_and_button_gets_the_space_it_asked_for(session):
    """The practical meaning of 'all components visible': no control is
    narrower than the text it has to draw, so nothing is cut off."""
    assert session.initial["squeezed"] == [], (
        f"开窗时有文字被压掉: {session.initial['squeezed']}")


def test_window_fits_on_the_desktop(session):
    width, height = session.initial["client"]
    screen_w, screen_h = session.initial["screen"]
    assert width <= screen_w - 40, "窗口不该超出桌面宽度"
    assert height <= screen_h - 60, "窗口不该超出桌面高度"


def test_window_is_at_least_the_preferred_size(session):
    width, height = session.initial["client"]
    assert width >= gui.PREFERRED_WIDTH
    assert height >= gui.PREFERRED_HEIGHT


def test_window_is_big_enough_for_its_own_contents(session):
    """The initial geometry must cover what the layout asked for."""
    width, height = session.initial["client"]
    need_w, need_h = session.initial["required"]
    assert width >= need_w or width >= session.initial["screen"][0] - 80
    assert height >= need_h or height >= session.initial["screen"][1] - 110


def test_minimum_size_protects_the_layout(session):
    """Shrinking too far is prevented rather than clipping widgets."""
    min_w, min_h = session.initial["minsize"]
    assert min_w >= 320 and min_h >= 320
    assert min_w <= session.initial["client"][0]
    assert min_h <= session.initial["client"][1]


def test_the_file_list_offers_horizontal_scrolling(session):
    """Long paths must be reachable instead of being silently cut off."""
    assert session.initial["has_hscroll"]


# --------------------------------------------------------------------------
# scaling with the window, without distortion
# --------------------------------------------------------------------------

@pytest.mark.parametrize("label", ["minimum", "large", "short"])
def test_nothing_is_clipped_at_other_window_sizes(session, label):
    state = session.sizes[label]
    assert state["clipped"] == [], (
        f"{label} ({state['client']}) 下有组件被裁掉: {state['clipped']}")


@pytest.mark.parametrize("label", ["minimum", "large", "short"])
def test_nothing_is_squeezed_at_other_window_sizes(session, label):
    state = session.sizes[label]
    assert state["squeezed"] == [], (
        f"{label} ({state['client']}) 下有文字被压掉: {state['squeezed']}")


def test_file_list_and_log_both_grow_with_the_window(session):
    if not session.can_grow:
        pytest.skip("这台机器的桌面放不下更大的窗口")
    base = session.initial
    large = session.sizes["large"]
    assert large["list"][1] > base["list"][1] + 20, "文件列表应随窗口变高"
    assert large["log"][1] > base["log"][1] + 20, "日志区应随窗口变高"


def test_expanding_areas_share_the_extra_space_proportionally(session):
    """Weights are 3:2, so neither pane should swallow everything."""
    if not session.can_grow:
        pytest.skip("这台机器的桌面放不下更大的窗口")
    base, large = session.initial, session.sizes["large"]
    grew_list = large["list"][1] - base["list"][1]
    grew_log = large["log"][1] - base["log"][1]
    assert min(grew_list, grew_log) > 0
    assert 0.4 < grew_list / grew_log < 6.0, "两块的伸缩比例应当大致相当"


def test_fixed_widgets_keep_their_natural_size(session):
    """Buttons must not be stretched with the window -- that is the distortion.

    Compared against each button's own requested size, so the check holds
    whatever the display scaling happens to be.
    """
    for label in ("initial", "large", "short", "minimum"):
        state = session.initial if label == "initial" else session.sizes[label]
        for key in ("button", "start_button"):
            actual, requested = state[key]
            assert actual == requested, (
                f"{label}: {key} 被拉伸了 ({actual} != {requested})")


def test_the_expanding_areas_fill_the_width(session):
    for label in ("minimum", "large", "short"):
        state = session.sizes[label]
        assert state["list"][0] > 300, f"{label}: 文件列表宽度异常"
        assert state["log"][0] > 300, f"{label}: 日志区宽度异常"


def test_hint_text_rewraps_instead_of_being_truncated(session):
    """The wrapped hint follows the window width rather than being cut off."""
    for label, state in [("initial", session.initial)] + list(session.sizes.items()):
        expected = max(320, state["client"][0] - 60)
        assert state["wrap"] == expected, f"{label}: 换行宽度没有跟随窗口宽度"
        assert state["wrap"] >= 320
    if session.can_grow:
        assert session.sizes["large"]["wrap"] > session.initial["wrap"]


# --------------------------------------------------------------------------
# the detectors above have to be able to fail
# --------------------------------------------------------------------------

class _Fake:
    """Minimal stand-in for a Tk widget, so the checks can be probed directly."""

    def __init__(self, klass, width=50, height=20, wanted_w=50, wanted_h=20,
                 x=0, y=0, children=()):
        self._klass, self._w, self._h = klass, width, height
        self._rw, self._rh = wanted_w, wanted_h
        self._x, self._y, self._children = x, y, list(children)

    def winfo_class(self) -> str:
        return self._klass

    def winfo_ismapped(self) -> bool:
        return True

    def winfo_width(self) -> int:
        return self._w

    def winfo_height(self) -> int:
        return self._h

    def winfo_reqwidth(self) -> int:
        return self._rw

    def winfo_reqheight(self) -> int:
        return self._rh

    def winfo_x(self) -> int:
        return self._x

    def winfo_y(self) -> int:
        return self._y

    def winfo_children(self):
        return list(self._children)

    def cget(self, _key):
        return "text"


def test_clipping_detector_is_not_vacuous(monkeypatch):
    import test_gui_layout as layout

    outside = _Fake("TLabel", width=90, height=20, x=60, y=0)      # 60+90 > 100
    inside = _Fake("TLabel", width=40, height=20, x=10, y=0)
    monkeypatch.setattr(layout, "_walk", lambda _root: [])
    parent = _Fake("TFrame", width=100, height=100)
    monkeypatch.setattr(parent, "winfo_children", lambda: [outside, inside])
    reported = layout._clipped(parent)
    assert len(reported) == 1 and reported[0]["class"] == "TLabel"

    ok_parent = _Fake("TFrame", width=100, height=100)
    monkeypatch.setattr(ok_parent, "winfo_children", lambda: [inside])
    assert layout._clipped(ok_parent) == []


def test_squeeze_detector_is_not_vacuous(monkeypatch):
    import test_gui_layout as layout

    squeezed = _Fake("TLabel", width=40, wanted_w=120)
    fine = _Fake("TLabel", width=200, wanted_w=120)
    ignored = _Fake("Listbox", width=10, wanted_w=500)     # not a text control
    monkeypatch.setattr(layout, "_walk", lambda _root: [squeezed, fine, ignored])
    assert layout._squeezed(None) == [
        {"class": "TLabel", "text": "text", "width": 40, "wanted": 120}]
