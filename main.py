from ui.ui_core import MainWindow
from core.dispatcher import Dispatcher
import sys
from PyQt6.QtWidgets import QApplication
from core.handlers import discover_handlers
from ui.widgets.logger import setup_logging
import logging
logger = logging.getLogger('radiata')


if __name__ == '__main__':

    discover_handlers() # import hack

    app = QApplication(sys.argv)
    print('Application Started.')
    # Initialize logger
    qt_log_handler = setup_logging(level=logging.DEBUG)
    # Initialize logic
    dispatcher = Dispatcher()
    # Initialize window
    window = MainWindow(dispatcher)

    qt_log_handler.log_signal.connect(window.workspace_page.log_console.append_log)

    window.show()
    exit_code = app.exec()

    qt_log_handler.close()
    logger.removeHandler(qt_log_handler)

    print('Application Closed Successfully.')
    sys.exit(exit_code)
