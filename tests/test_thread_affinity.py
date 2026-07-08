import threading
from PyQt6.QtCore import QObject, QThread, pyqtSignal, QTimer
from PyQt6.QtWidgets import QApplication

def test_bound_method_runs_on_main_lambda_runs_on_worker(qapp):
    main_id = threading.get_ident()
    seen = {}
    class Emitter(QObject):
        sig = pyqtSignal()
    class Recv(QObject):
        def slot(self): seen['bound'] = threading.get_ident()
    recv = Recv()
    class W(QThread):
        def run(self):
            e = Emitter()
            e.sig.connect(recv.slot)                       # bound → main
            e.sig.connect(lambda: seen.__setitem__('lambda', threading.get_ident()))  # functor → worker
            e.sig.emit(); self.msleep(200)
    w = W(); w.finished.connect(qapp.quit); w.start()
    QTimer.singleShot(2000, qapp.quit); qapp.exec()
    assert seen['lambda'] != main_id      # lambda ran on worker (the bug pattern)
    assert seen['bound'] == main_id       # bound method ran on main (the fix pattern)
