"""Runs one question in the background, so the page stays responsive and can offer Stop.

A stopped job is not killed: a model call that has started cannot be recalled. Stop sets a flag that the engine
checks before it saves anything, and the page drops the job, so the answer is never shown, saved or remembered.
"""
from __future__ import annotations

import threading
from typing import Any, Callable


class Job:
    def __init__(self, ident: str, fn: Callable[[threading.Event], Any]):
        self.id = ident
        self.cancel = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None
        self._done = threading.Event()
        threading.Thread(target=self._run, args=(fn,), daemon=True).start()

    def _run(self, fn):
        try:
            self.result = fn(self.cancel)
        except BaseException as e:      # handed to the page, which decides what to show
            self.error = e
        finally:
            self._done.set()

    def done(self) -> bool:
        return self._done.is_set()

    def stop(self) -> None:
        self.cancel.set()

    @property
    def stopped(self) -> bool:
        return self.cancel.is_set()
