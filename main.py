import sys
from PyQt6.QtWidgets import QApplication
from core.dispatcher import Dispatcher
from core.registry import discover_all, Registry
from ui.theme_manager import ThemeManager
from ui.ui_core import MainWindow

from ui.logger import setup_logging
import logging
logger = logging.getLogger('radiata')


if __name__ == '__main__':
    self_test = '--self-test' in sys.argv

    discover_all()
    Registry.summary()

    app = QApplication(sys.argv)
    ThemeManager.initialize(app)
    qt_log_handler = setup_logging()
    dispatcher = Dispatcher()
    window = MainWindow(dispatcher)
    qt_log_handler.log_signal.connect(window.workspace_page.log_console.append_log)

    if self_test:
        # CI smoke: construct everything, do not enter the interactive loop.
        print('Self-test OK.')
        qt_log_handler.close()
        logger.removeHandler(qt_log_handler)
        sys.exit(0)

    window.show()
    exit_code = app.exec()
    qt_log_handler.close()
    logger.removeHandler(qt_log_handler)
    print('Application Closed Successfully.')
    sys.exit(exit_code)
