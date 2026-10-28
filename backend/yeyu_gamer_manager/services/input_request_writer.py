import threading


class InputRequestWriter(threading.Thread):
    def __init__(self, pipe, payload, attempt_id):
        super().__init__(name=f"yeyu-input-request-{attempt_id[:8]}", daemon=True)
        self._pipe = pipe
        self._payload = payload
        self.error = None

    def run(self):
        try:
            if self._pipe.write(self._payload) != len(self._payload):
                raise OSError("Incomplete fixed Host input request")
            self._pipe.flush()
        except BaseException as error:
            self.error = error


class DurableCancelWriter(threading.Thread):
    def __init__(self, operation, attempt_id):
        super().__init__(name=f"yeyu-cancel-write-{attempt_id[:8]}", daemon=True)
        self._operation = operation
        self.result = False
        self.error = None

    def run(self):
        try:
            self.result = self._operation() is True
        except BaseException as error:
            self.error = error
        finally:
            self._operation = None
