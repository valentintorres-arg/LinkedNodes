"""The window's own title bar: its menus and its buttons drawn in the page, as code editors do.

The system's caption and its menu strip are light, tall and not ours to style. So the
caption is taken off, and a bar in the page takes its place on every page the window
shows. The frame itself stays: the window is still resized by its edges, snapped to the
sides of the screen, and maximized by a double click on the bar, because the bar is
declared to the web view as the place a caption would be.
"""
from __future__ import annotations

import ctypes
import html
import json
import os

HEIGHT = 32  # pixels; the pages leave this much room at their top
STYLE = """
#ln-bar { position: fixed; top: 0; left: 0; right: 0; height: %(height)dpx; z-index: 1000; display: flex;
  align-items: stretch; background: #181818; border-bottom: 1px solid #2b2b2b; color: #ccc; user-select: none;
  font: 13px/1 "Segoe UI", system-ui, sans-serif; app-region: drag; -webkit-app-region: drag; box-sizing: border-box; }
#ln-bar * { box-sizing: border-box; }
#ln-bar svg { width: 16px; height: 16px; margin: auto 6px auto 10px; flex: none; }
#ln-bar .ln-menus, #ln-bar .ln-buttons { display: flex; app-region: no-drag; -webkit-app-region: no-drag; }
#ln-bar .ln-menu { position: relative; display: flex; align-items: center; }
#ln-bar .ln-menu > button { font: inherit; color: inherit; background: none; border: 0; height: 22px; padding: 0 8px;
  border-radius: 5px; cursor: default; }
#ln-bar .ln-menu > button:hover, #ln-bar .ln-menu.open > button { background: rgba(90, 93, 94, .31); color: #fff; }
#ln-bar .ln-list { display: none; position: absolute; top: %(drop)dpx; left: 0; min-width: 240px; padding: 4px;
  background: #1f1f1f; border: 1px solid #454545; border-radius: 5px; box-shadow: 0 2px 8px rgba(0, 0, 0, .36); }
#ln-bar .ln-menu.open .ln-list { display: block; }
#ln-bar .ln-list button { display: block; width: 100%%; height: 26px; padding: 0 26px; font: inherit; color: #ccc;
  background: none; border: 0; border-radius: 4px; text-align: left; white-space: nowrap; cursor: default;
  position: relative; }
#ln-bar .ln-list button:hover, #ln-bar .ln-list button:focus-visible { background: #0078d4; color: #fff; outline: none; }
#ln-bar .ln-list button.on::before { content: "\\2713"; position: absolute; left: 9px; }
#ln-bar .ln-list hr { border: 0; border-top: 1px solid #454545; margin: 4px 0; }
#ln-bar .ln-title { flex: 1; min-width: 0; display: flex; align-items: center; justify-content: center; padding: 0 12px;
  font-size: 12px; color: #9d9d9d; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#ln-bar .ln-buttons button { width: 46px; height: 100%%; padding: 0; border: 0; background: none; color: #ccc; cursor: default;
  font: 10px/1 "Segoe Fluent Icons", "Segoe MDL2 Assets", sans-serif; }
#ln-bar .ln-buttons button:hover { background: rgba(255, 255, 255, .1); color: #fff; }
#ln-bar .ln-buttons button.ln-close:hover { background: #e81123; }
""" % {"height": HEIGHT, "drop": HEIGHT - 5}
LOGO = ("<svg viewBox='0 0 16 16' aria-hidden='true'><g stroke='#fff' stroke-width='1.1'>"
        "<path d='M4.5 5 11.5 4M4.5 5 8.8 9.8M11.5 4 8.8 9.8M8.8 9.8 4 12.2M8.8 9.8 12.5 12.2'/></g>"
        "<g fill='#fff'><circle cx='4.5' cy='5' r='1.7'/><circle cx='11.5' cy='4' r='1.7'/>"
        "<circle cx='4' cy='12.2' r='1.7'/><circle cx='12.5' cy='12.2' r='1.7'/></g>"
        "<circle cx='8.8' cy='9.8' r='2.3' fill='#b9a8ff'/></svg>")
# What the bar does once it is in the page. A menu opens on a click and then follows the
# pointer; anything else that is clicked, Escape, or the window losing focus closes it.
BEHAVIOUR = """
(() => {
  const old = document.getElementById('ln-bar');
  if (old) old.remove();
  if (!document.getElementById('ln-style'))
    document.head.append(Object.assign(document.createElement('style'), {id: 'ln-style', textContent: STYLE}));
  document.documentElement.style.setProperty('--chrome', HEIGHT + 'px');
  const bar = Object.assign(document.createElement('div'), {id: 'ln-bar', innerHTML: MARKUP});
  document.body.append(bar);
  const menus = [...bar.querySelectorAll('.ln-menu')];
  const close = () => menus.forEach(menu => menu.classList.remove('open'));
  for (const menu of menus) {
    const title = menu.firstElementChild;
    title.addEventListener('click', () => { const open = menu.classList.contains('open'); close(); menu.classList.toggle('open', !open); });
    title.addEventListener('pointerenter', () => { if (bar.querySelector('.ln-menu.open')) { close(); menu.classList.add('open'); } });
  }
  bar.addEventListener('click', event => {
    const item = event.target.closest('[data-do]');
    if (!item) return;
    close();
    const ask = window.pywebview && window.pywebview.api && window.pywebview.api[item.dataset.do];
    if (ask) ask(...(item.dataset.arg ? [item.dataset.arg] : [])).then(shape);
  });
  document.addEventListener('pointerdown', event => { if (!event.target.closest('.ln-menu')) close(); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') close(); });
  window.addEventListener('blur', close);
  // The middle button shows what it will do: restore a window that fills the screen, or make it fill it.
  const size = bar.querySelector('.ln-size');
  function shape() {
    const ask = size && window.pywebview && window.pywebview.api && window.pywebview.api.zoomed;
    if (ask) ask().then(zoomed => { size.textContent = zoomed ? '\\uE923' : '\\uE922'; });
  }
  window.addEventListener('resize', shape);
  window.addEventListener('pywebviewready', shape);
  shape();
})()
"""


def script(menus: list, title: str, buttons: dict | None) -> str:
    """The code that puts the bar into the page on show.

    menus is a list of (name, items); an item is (name, what to ask of the program, its
    argument or '', whether it is ticked), or None for a line between two groups.
    buttons names the three window buttons, or is None where the system keeps its own.
    """
    parts = [LOGO, "<div class='ln-menus'>"]
    for name, items in menus:
        parts.append(f"<div class='ln-menu'><button>{html.escape(name)}</button><div class='ln-list'>")
        for item in items:
            if item is None:
                parts.append("<hr>")
                continue
            label, do, arg, on = item
            tick = " class='on'" if on else ""
            parts.append(f"<button data-do='{do}' data-arg='{html.escape(arg, quote=True)}'{tick}>"
                         f"{html.escape(label)}</button>")
        parts.append("</div></div>")
    parts.append(f"</div><div class='ln-title pywebview-drag-region'>{html.escape(title)}</div>")
    if buttons:
        parts.append(
            "<div class='ln-buttons'>"
            f"<button data-do='minimize' title='{html.escape(buttons['minimize'], quote=True)}'>&#xE921;</button>"
            f"<button data-do='maximize' class='ln-size' title='{html.escape(buttons['maximize'], quote=True)}'>&#xE922;</button>"
            f"<button data-do='close' class='ln-close' title='{html.escape(buttons['close'], quote=True)}'>&#xE8BB;</button>"
            "</div>")
    names = f"const STYLE = {json.dumps(STYLE)}, MARKUP = {json.dumps(''.join(parts))}, HEIGHT = {HEIGHT};"
    return "(() => {" + names + BEHAVIOUR + "})()"


# ---- Windows: the caption comes off, the frame stays
_WM_NCCALCSIZE, _WM_NCDESTROY, _WM_MOVED, _WNDPROC = 0x0083, 0x0082, 0x0047, -4
_kept: dict[int, tuple] = {}  # per window: our procedure, kept alive, and the one it stands in for


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    user32.CallWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.CallWindowProcW.restype = ctypes.c_ssize_t
    user32.IsZoomed.argtypes = user32.IsIconic.argtypes = [ctypes.c_void_p]
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Rect)]
    user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_int] * 4 + [ctypes.c_uint]
    return user32


def unframe(window) -> None:
    """Take the caption off a window that is about to be shown. Must run on the thread that owns it.

    The window keeps its style, so the system goes on treating it as a framed one. Only the
    answer to "where does the client area begin" changes: at the very top, instead of under
    a caption. A maximized window hangs over the screen by the width of its frame, so there
    the top is brought back in by that much.

    The toolkit under the window still believes in the caption. When the window comes back
    from filling the screen, or from the taskbar, it sizes it for the client area it had
    plus a caption, so the window would grow by one caption every time. The size it had
    before is therefore remembered and put back.
    """
    if os.name != "nt":
        return
    form, user32 = window.native, _user32()
    handle = form.Handle.ToInt64()
    frame = user32.GetSystemMetrics(33) + user32.GetSystemMetrics(92)  # the sizing border and its padding
    size = {"usual": None, "away": False}  # its size as an ordinary window, and whether it has left that state

    def procedure(hwnd, message, wparam, lparam):
        before = _kept[handle][1]
        if before is None:  # not ours to answer yet
            return 0
        if message == _WM_NCCALCSIZE and wparam:
            area = ctypes.cast(lparam, ctypes.POINTER(_Rect)).contents
            top = area.top
            user32.CallWindowProcW(before, hwnd, message, wparam, lparam)  # the sides and the bottom, as usual
            area.top = top + (frame if user32.IsZoomed(hwnd) else 0)
            return 0
        if message == _WM_MOVED:
            result = user32.CallWindowProcW(before, hwnd, message, wparam, lparam)
            if user32.IsZoomed(hwnd) or user32.IsIconic(hwnd):
                size["away"] = True
            elif size["away"] and size["usual"]:
                size["away"] = False  # where it lands is the system's to say, as when it is dragged off the top
                user32.SetWindowPos(hwnd, None, 0, 0, *size["usual"], 0x0002 | 0x0004 | 0x0010)
            else:
                now = _Rect()
                user32.GetWindowRect(hwnd, ctypes.byref(now))
                size["usual"] = (now.right - now.left, now.bottom - now.top)
            return result
        if message == _WM_NCDESTROY:
            user32.SetWindowLongPtrW(hwnd, _WNDPROC, before)
            _kept.pop(handle, None)
        return user32.CallWindowProcW(before, hwnd, message, wparam, lparam)

    ours = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)(procedure)
    _kept[handle] = (ours, None)
    _kept[handle] = (ours, user32.SetWindowLongPtrW(handle, _WNDPROC, ctypes.cast(ours, ctypes.c_void_p)))
    user32.SetWindowPos(handle, None, 0, 0, 0, 0, 0x0020 | 0x0002 | 0x0001 | 0x0004 | 0x0010)  # the frame changed, nothing else
    dark = ctypes.c_int(1)  # the thin border the system still draws follows the dark theme
    ctypes.WinDLL("dwmapi").DwmSetWindowAttribute(ctypes.c_void_p(handle), 20, ctypes.byref(dark), 4)

    def ready(sender, args):  # lets the page say which part of it is the caption: app-region: drag
        try:
            form.webview.CoreWebView2.Settings.IsNonClientRegionSupportEnabled = True
        except Exception:  # an older web view: the bar still works, it only cannot be dragged
            pass

    form.webview.CoreWebView2InitializationCompleted += ready


def zoomed(window) -> bool:
    """Whether the window fills the screen."""
    return os.name == "nt" and bool(_user32().IsZoomed(window.native.Handle.ToInt64()))
