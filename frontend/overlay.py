"""
Visible local AI desktop assistant overlay (PyQt5).

  * Always-on-top, frameless, semi-transparent background.
  * Movable, resizable, scrollable, adjustable opacity + font size.
  * Ctrl+Shift+H hotkey toggles visibility.
  * Subscribes to ws://localhost:8765/ws and renders answers live.
"""
from __future__ import annotations

import json
import sys
from typing import Optional

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import QCoreApplication

try:
    import websocket  # type: ignore
except ImportError:  # pragma: no cover
    websocket = None  # we'll degrade gracefully if missing

from config.settings import Settings


# ---- Windows API: hide window from screen capture ---------------

def hide_from_capture(hwnd: int) -> bool:
    """
    Set WDA_EXCLUDEFROMCAPTURE on the given HWND so the window is
    invisible to screen-share / capture APIs on Windows 10 1903+.

    Falls back to WDA_MONITOR (older Win10) if the new flag isn't
    supported, which still hides it from most meeting apps.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        # 0x00000011 = WDA_EXCLUDEFROMCAPTURE
        # 0x00000001 = WDA_MONITOR (older fallback)
        for flag in (0x11, 0x01):
            result = user32.SetWindowDisplayAffinity(wintypes.HWND(hwnd), wintypes.DWORD(flag))
            if bool(result):
                print(f"[overlay] capture-affinity set with flag=0x{flag:X}")
                return True
        print("[overlay] SetWindowDisplayAffinity failed; overlay will be visible in shares.")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"[overlay] hide_from_capture error: {exc}")
        return False


# ---- Custom text widget that supports HTML + auto-scroll --------

class AnswerView(QtWidgets.QTextBrowser):
    """Read-only rich-text view that auto-scrolls to the latest answer."""

    def __init__(self) -> None:
        super().__init__()
        self.setOpenExternalLinks(False)
        self.setReadOnly(True)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.append_html("<i style='color:#888'>Ask a local model a question below…</i>")

    def append_html(self, html: str) -> None:
        self.append(html)
        # Auto-scroll to bottom
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())

    def replace_last_line(self, html: str) -> None:
        """
        Replace the last block (paragraph) of the document with `html`.
        Used to update a 'live' partial-transcript line in place.
        """
        cursor = self.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        cursor.movePosition(QtGui.QTextCursor.StartOfBlock,
                            QtGui.QTextCursor.KeepAnchor)
        cursor.movePosition(QtGui.QTextCursor.PreviousBlock,
                            QtGui.QTextCursor.KeepAnchor)
        if not cursor.hasSelection():
            cursor.movePosition(QtGui.QTextCursor.StartOfBlock,
                                QtGui.QTextCursor.KeepAnchor)
            cursor.movePosition(QtGui.QTextCursor.EndOfBlock,
                                QtGui.QTextCursor.KeepAnchor)
        cursor.insertHtml(html)
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())

    def clear_answers(self) -> None:
        self.clear()
        self.append_html("<i style='color:#888'>Cleared.</i>")


# ---- Main overlay window ----------------------------------------

class OverlayWindow(QtWidgets.QWidget):
    """
    The floating local AI desktop assistant.
    """

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.selected_model = ""
        self.current_answer_text = ""
        self._drag_pos: Optional[QtCore.QPoint] = None
        self._resize_start_geometry: Optional[QtCore.QRect] = None
        self._build_ui()
        self._apply_window_flags()
        self.setWindowOpacity(settings.overlay_opacity)

        # Show the window and exclude it from screen capture.
        self.show()
        QtCore.QTimer.singleShot(50, self._apply_capture_affinity)

        # Load models on startup
        QtCore.QTimer.singleShot(100, self.load_models)

    def _apply_capture_affinity(self) -> None:
        hwnd = int(self.winId())
        hide_from_capture(hwnd)

    # -- UI --------------------------------------------------------

    def _build_ui(self) -> None:
        self.setWindowTitle("Local AI Desktop Assistant")
        self.resize(720, 450)

        # Semi-transparent dark background.
        self.setStyleSheet(
            "QWidget#Root { background-color: rgba(20, 20, 24, 235); "
            "border: 1px solid rgba(255,255,255,40); border-radius: 8px; }"
            "QTextBrowser { background: transparent; color: #f5f5f5; }"
            "QToolBar { background: transparent; border: none; }"
            "QToolButton { color: #ddd; padding: 2px 6px; }"
            "QToolButton:hover { color: #fff; }"
        )

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        container = QtWidgets.QWidget()
        container.setObjectName("Root")
        root.addWidget(container)

        v = QtWidgets.QVBoxLayout(container)
        v.setContentsMargins(8, 4, 8, 8)

        # Grab / Tool Bar
        toolbar = QtWidgets.QToolBar()
        toolbar.setMovable(False)
        toolbar.setIconSize(QtCore.QSize(12, 12))
        
        # Model selection dropdown
        self.model_combo = QtWidgets.QComboBox()
        self.model_combo.setMinimumWidth(180)
        self.model_combo.setStyleSheet(
            "QComboBox { background-color: rgba(30, 30, 35, 200); color: #ddd; "
            "border: 1px solid rgba(255,255,255,40); border-radius: 4px; "
            "padding: 2px 6px; }"
            "QComboBox QAbstractItemView { background-color: #222; color: #ddd; }"
        )
        self.model_combo.addItem("Loading local models...")
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        
        toolbar.addWidget(self.model_combo)
        toolbar.addSeparator()

        self.answer_view = AnswerView()
        v.addWidget(toolbar)
        v.addWidget(self.answer_view, 1)

        # Actions
        self.act_clear = toolbar.addAction("Clear")
        self.act_smaller = toolbar.addAction("A-")
        self.act_bigger = toolbar.addAction("A+")
        self.act_opacity_down = toolbar.addAction("◐")
        self.act_opacity_up = toolbar.addAction("◑")
        self.act_hide = toolbar.addAction("Hide (Ctrl+Shift+H)")

        self.act_clear.triggered.connect(self.answer_view.clear_answers)
        self.act_smaller.triggered.connect(lambda: self._bump_font(-1))
        self.act_bigger.triggered.connect(lambda: self._bump_font(+1))
        self.act_opacity_down.triggered.connect(lambda: self._bump_opacity(-0.1))
        self.act_opacity_up.triggered.connect(lambda: self._bump_opacity(+0.1))
        self.act_hide.triggered.connect(self.toggle_visibility)

        # Input Box + Ask Button at bottom
        input_layout = QtWidgets.QHBoxLayout()
        input_layout.setSpacing(6)
        
        self.input_query = QtWidgets.QLineEdit()
        self.input_query.setPlaceholderText("Ask a question…")
        self.input_query.setStyleSheet(
            "QLineEdit { background-color: rgba(30, 30, 35, 200); color: #fff; "
            "border: 1px solid rgba(255,255,255,40); border-radius: 4px; "
            "padding: 6px 10px; }"
            "QLineEdit:focus { border: 1px solid #9ec5ff; }"
        )
        self.input_query.returnPressed.connect(self.submit_question)
        
        self.btn_ask = QtWidgets.QPushButton("Ask")
        self.btn_ask.setStyleSheet(
            "QPushButton { background-color: #2b5c8f; color: #fff; border: none; "
            "border-radius: 4px; padding: 6px 12px; font-weight: bold; }"
            "QPushButton:hover { background-color: #3b74b3; }"
            "QPushButton:disabled { background-color: #444; color: #888; }"
        )
        self.btn_ask.clicked.connect(self.submit_question)
        
        input_layout.addWidget(self.input_query, 1)
        input_layout.addWidget(self.btn_ask)
        v.addLayout(input_layout)

        # Apply initial font size
        self._apply_font_size(self.settings.overlay_font_size)

    def _apply_window_flags(self) -> None:
        flags = (
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.Tool
        )
        self.setWindowFlags(flags)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)

    # -- model handling -------------------------------------------

    def load_models(self) -> None:
        def fetch():
            try:
                import requests
                url = f"http://{self.settings.backend_host}:{self.settings.backend_port}/api/models"
                resp = requests.get(url, timeout=4.0)
                if resp.status_code == 200:
                    models = resp.json()
                    QtCore.QMetaObject.invokeMethod(self, "_update_models_ui", QtCore.Qt.QueuedConnection, QtCore.Q_ARG(list, models))
                else:
                    QtCore.QMetaObject.invokeMethod(self, "_update_models_error", QtCore.Qt.QueuedConnection)
            except Exception:
                QtCore.QMetaObject.invokeMethod(self, "_update_models_error", QtCore.Qt.QueuedConnection)

        import threading
        threading.Thread(target=fetch, daemon=True).start()

    @QtCore.pyqtSlot(list)
    def _update_models_ui(self, models: list) -> None:
        self.model_combo.clear()
        if not models:
            self.model_combo.addItem("No local models found — is Ollama running?")
            return
        for m in models:
            name = m.get("name", "unknown")
            self.model_combo.addItem(name, name)
        
        # Set to settings default model if available
        default_model = self.settings.ollama_model
        index = self.model_combo.findData(default_model)
        if index >= 0:
            self.model_combo.setCurrentIndex(index)

    @QtCore.pyqtSlot()
    def _update_models_error(self) -> None:
        self.model_combo.clear()
        self.model_combo.addItem("No local models found — is Ollama running?")

    def _on_model_changed(self) -> None:
        val = self.model_combo.currentData()
        if val:
            self.selected_model = val
        else:
            self.selected_model = ""

    # -- question asking -------------------------------------------

    def submit_question(self) -> None:
        query = self.input_query.text().strip()
        if not query:
            return
        
        self.input_query.setEnabled(False)
        self.btn_ask.setEnabled(False)
        self.btn_ask.setText("Thinking…")

        def post_ask():
            try:
                import requests
                url = f"http://{self.settings.backend_host}:{self.settings.backend_port}/api/ask"
                payload = {
                    "question": query,
                    "model": self.selected_model
                }
                requests.post(url, json=payload, timeout=60.0)
            except Exception as e:
                print("Error posting question:", e)
            finally:
                QtCore.QMetaObject.invokeMethod(self, "_on_ask_complete", QtCore.Qt.QueuedConnection)

        import threading
        threading.Thread(target=post_ask, daemon=True).start()

    @QtCore.pyqtSlot()
    def _on_ask_complete(self) -> None:
        self.input_query.setEnabled(True)
        self.btn_ask.setEnabled(True)
        self.btn_ask.setText("Ask")
        self.input_query.clear()
        self.input_query.setFocus()

    # -- font / opacity --------------------------------------------

    def _apply_font_size(self, size: int) -> None:
        size = max(8, min(size, 40))
        self.settings.overlay_font_size = size
        font = self.answer_view.font()
        font.setPointSize(size)
        self.answer_view.setFont(font)
        self.answer_view.setStyleSheet(
            f"QTextBrowser {{ background: transparent; color: #f5f5f5; "
            f"font-size: {size}pt; }}"
        )

    def _bump_font(self, delta: int) -> None:
        self._apply_font_size(self.settings.overlay_font_size + delta)

    def _bump_opacity(self, delta: float) -> None:
        new_op = max(0.2, min(1.0, self.windowOpacity() + delta))
        self.setWindowOpacity(new_op)
        self.settings.overlay_opacity = new_op

    # -- drag / resize --------------------------------------------

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton:
            self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._drag_pos and event.buttons() & QtCore.Qt.LeftButton:
            self.move(event.globalPos() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        self._drag_pos = None

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.pos().y() < 28:
            self.toggle_visibility()
        else:
            super().mouseDoubleClickEvent(event)

    # -- public ----------------------------------------------------

    def toggle_visibility(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self._apply_capture_affinity()

    def render_payload(self, payload: dict) -> None:
        kind = payload.get("type")
        if kind == "stream_start":
            q = payload.get("question", "").strip()
            self.current_answer_text = ""
            self.answer_view.append_html(f"<div style='color:#9ec5ff'><b>Q:</b> {self._esc(q)}</div>")
            self.answer_view.append_html("<div style='color:#f5f5f5; margin-top:4px'><b>A:</b> <i>thinking…</i></div>")
        elif kind == "token":
            text = payload.get("text", "")
            self.current_answer_text += text
            self.answer_view.replace_last_line(f"<div style='color:#f5f5f5; margin-top:4px'><b>A:</b> {self._esc(self.current_answer_text)}</div>")
        elif kind == "stream_end":
            src = payload.get("source", "ollama")
            self.answer_view.replace_last_line(
                f"<div style='color:#f5f5f5; margin-top:4px'><b>A:</b> {self._esc(self.current_answer_text)}</div>"
                f"<div style='color:#666; font-size:small; margin-top:2px'>via {self._esc(src)}</div>"
                "<hr style='border: 0; border-top: 1px solid #333;'/>"
            )
        elif kind == "audio_source":
            pass
        elif kind == "state":
            pass

    @staticmethod
    def _esc(text: str) -> str:
        return (
            text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\n", "<br/>")
        )


# ---- Hotkey (global) --------------------------------------------

class GlobalHotkey(QtCore.QObject):
    triggered = QtCore.pyqtSignal()

    VK_H = 0x48
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_NOREPEAT = 0x4000

    def __init__(self, hotkey_id: int = 1, parent: QtCore.QObject = None) -> None:
        super().__init__(parent)
        self._hotkey_id = hotkey_id
        self._hwnd = None
        self._registered = False
        self._filter = _HotkeyEventFilter(self)

    def register(self, hwnd: int, vk: int, modifiers: int) -> bool:
        if sys.platform != "win32":
            print("[hotkey] global hotkey only implemented on Windows.")
            return False
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL

        ok = bool(user32.RegisterHotKey(wintypes.HWND(hwnd), self._hotkey_id,
                                         int(modifiers) | self.MOD_NOREPEAT, int(vk)))
        if not ok:
            err = ctypes.GetLastError()
            print(f"[hotkey] RegisterHotKey failed; GetLastError={err}")
            return False

        self._hwnd = hwnd
        self._registered = True

        QCoreApplication.instance().installNativeEventFilter(self._filter)
        print(f"[hotkey] Ctrl+Shift+H registered (hwnd={hwnd}, id={self._hotkey_id})")
        return True

    def unregister(self) -> None:
        if not self._registered or sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes
        ctypes.windll.user32.UnregisterHotKey(wintypes.HWND(self._hwnd), self._hotkey_id)
        self._registered = False

    def _on_hotkey(self) -> None:
        QtCore.QMetaObject.invokeMethod(
            self, "_emit_triggered", QtCore.Qt.QueuedConnection
        )

    @QtCore.pyqtSlot()
    def _emit_triggered(self) -> None:
        self.triggered.emit()


class _HotkeyEventFilter(QtCore.QAbstractNativeEventFilter):
    def __init__(self, owner: GlobalHotkey) -> None:
        super().__init__()
        self._owner = owner

    def nativeEventFilter(self, event_type, message):  # noqa: N802
        if not self._owner._registered or sys.platform != "win32":
            return False, 0
        try:
            import ctypes
            from ctypes import wintypes

            WM_HOTKEY = 0x0312
            msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
            if msg.message == WM_HOTKEY and msg.wParam == self._owner._hotkey_id:
                self._owner._on_hotkey()
                return True, 0
        except Exception:
            pass
        return False, 0


# ---- WebSocket thread -------------------------------------------

class WsClient(QtCore.QObject):
    new_payload = QtCore.pyqtSignal(dict)
    state = QtCore.pyqtSignal(str)

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url
        self._thread = QtCore.QThread()
        self._thread.run = self._run  # type: ignore[assignment]
        self._stop = False

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        self._thread.quit()
        self._thread.wait(2000)

    def _run(self) -> None:
        if websocket is None:
            self.state.emit("websocket-client not installed (pip install websocket-client)")
            return
        backoff = 1.0
        while not self._stop:
            try:
                self.state.emit(f"connecting {self.url}")
                ws = websocket.WebSocket()
                ws.connect(self.url)
                self.state.emit("connected")
                backoff = 1.0
                while not self._stop:
                    raw = ws.recv()
                    if not raw:
                        break
                    try:
                        self.new_payload.emit(json.loads(raw))
                    except json.JSONDecodeError:
                        pass
                ws.close()
            except Exception as exc:  # noqa: BLE001
                self.state.emit(f"disconnected: {exc.__class__.__name__}")
                import time
                time.sleep(min(backoff, 10))
                backoff *= 1.5


# ---- Entry point --------------------------------------------------

def main() -> int:
    settings = Settings()
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Local AI Desktop Assistant")

    win = OverlayWindow(settings)

    hotkey = GlobalHotkey(parent=app)
    QtCore.QTimer.singleShot(
        100,
        lambda: hotkey.register(int(win.winId()), GlobalHotkey.VK_H,
                                GlobalHotkey.MOD_CONTROL | GlobalHotkey.MOD_SHIFT),
    )
    hotkey.triggered.connect(win.toggle_visibility)

    ws_url = f"ws://{settings.backend_host}:{settings.backend_port}/ws"
    client = WsClient(ws_url)

    def on_payload(payload: dict) -> None:
        win.render_payload(payload)

    def on_state(msg: str) -> None:
        win.render_payload({"type": "state", "state": msg})

    client.new_payload.connect(on_payload)
    client.state.connect(on_state)
    client.start()

    print(f"[overlay] backend WS: {ws_url}")
    print("[overlay] hotkey: Ctrl+Shift+H to show/hide")
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
