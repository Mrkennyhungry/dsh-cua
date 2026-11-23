"""A bounded apartment worker whose blocked provider cannot grow a thread pool."""
from concurrent.futures import Future, TimeoutError
import queue
import threading

from .targets import RuntimeFault


class ApartmentWorker:
    def __init__(self, name, cleanup=None):
        self.name = name
        self.cleanup = cleanup
        self._queue = queue.Queue(maxsize=1)
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self.running = False

    def _run(self):
        import pythoncom
        pythoncom.CoInitialize()
        try:
            while not self._stopped.is_set():
                try:
                    future, function, args = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if not future.set_running_or_notify_cancel():
                    continue
                self.running = True
                try:
                    future.set_result(function(*args))
                except BaseException as error:
                    future.set_exception(error)
                finally:
                    self.running = False
        finally:
            while not self._queue.empty():
                future, _, _ = self._queue.get_nowait()
                future.cancel()
            if self.cleanup:
                self.cleanup()
            pythoncom.CoUninitialize()

    def call(self, function, *args, timeout=3):
        if self._thread is not None and self._thread.ident == threading.get_ident():
            return function(*args)
        future = Future()
        with self._lock:
            if self._stopped.is_set():
                raise RuntimeFault('BACKEND_UNAVAILABLE', f'{self.name} worker has stopped')
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
                self._thread.start()
            try:
                self._queue.put_nowait((future, function, args))
            except queue.Full:
                raise RuntimeFault('BACKEND_BUSY', f'{self.name} worker still has unfinished work')
        try:
            return future.result(timeout=timeout)
        except TimeoutError as error:
            if future.done():
                return future.result()
            future.cancel()
            raise RuntimeFault('BACKEND_TIMEOUT', f'{self.name} collection exceeded {timeout}s. Worker running: {self.running}') from error

    def shutdown(self, timeout=1):
        self._stopped.set()
        if self._thread is not None and self._thread.ident != threading.get_ident():
            self._thread.join(timeout=timeout)
