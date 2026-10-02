"""Фоновые задачи: работа в потоке, результат и прогресс — в главном потоке Qt."""

import threading
import traceback

from PySide6.QtCore import QObject, Signal, Qt


class _Bridge(QObject):
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(lambda fn: fn(), Qt.QueuedConnection)


_bridge = None


def post(fn):
    """Выполнить fn() в главном потоке (можно вызывать из любого потока)."""
    _bridge.call.emit(fn)


def init():
    global _bridge
    if _bridge is None:
        _bridge = _Bridge()


def run(fn, done=None, error=None):
    """fn() в потоке; done(результат) или error(исключение) — в главном потоке."""
    def work():
        try:
            r = fn()
        except Exception as e:                           # noqa: BLE001
            traceback.print_exc()
            if error is not None:
                post(lambda e=e: error(e))
            return
        if done is not None:
            post(lambda: done(r))
    th = threading.Thread(target=work, daemon=True)
    th.start()
    return th
