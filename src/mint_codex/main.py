from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from .core.controller import CodexController
from .ui.main_window import MainWindow


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = QApplication(sys.argv)
    controller = CodexController()
    window = MainWindow(controller)
    window.show()
    controller.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
