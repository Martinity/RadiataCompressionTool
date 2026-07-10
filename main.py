import sys
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon
from core.dispatcher import Dispatcher
from core.registry import discover_all
from ui.theme_manager import ThemeManager
from ui.ui_core import MainWindow

from ui.logger import setup_logging
import logging
logger = logging.getLogger('radiata')

if sys.platform == 'win32':
    import ctypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('com.radiata.moddingtool')
    except Exception:
        pass

if __name__ == '__main__':
    self_test = '--self-test' in sys.argv

    discover_all()
    app = QApplication(sys.argv)

    base_dir = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    icon_path = base_dir / 'ui' / 'assets' / 'app_icon.ico'
    if icon_path.exists():
        app_icon = QIcon(str(icon_path))
        app.setWindowIcon(app_icon)
    else:
        print(f'DEBUG Runtime graphic asset missing at: {icon_path}')

    ThemeManager.initialize(app)
    qt_log_handler = setup_logging()
    dispatcher = Dispatcher()
    window = MainWindow(dispatcher, is_test=self_test)
    qt_log_handler.log_signal.connect(window.workspace_page.log_console.append_log)

    if self_test:
        # CI smoke: construct everything, do not enter the interactive loop.
        print('Self-test OK.')
        qt_log_handler.close()
        logger.removeHandler(qt_log_handler)
        # Force cleanup C++ to prevent segfault
        del window
        del dispatcher
        del app
        sys.exit(0)

    window.show()
    exit_code = app.exec()
    qt_log_handler.close()
    logger.removeHandler(qt_log_handler)
    print('Application Closed Successfully.')
    sys.exit(exit_code)
