"""Bound remote stop polling between inputs; input boundaries always refresh."""
import time


class StopQuery:
    def __init__(self, read, *, interval=.05, clock=time.monotonic):
        self._read, self._clock = read, clock
        self._interval = interval
        self._next_read = float('-inf')
        self._stopped = False

    def __call__(self):
        if not self._stopped and self._clock() >= self._next_read:
            return self.refresh()
        return self._stopped

    def refresh(self):
        if not self._stopped:
            self._stopped = bool(self._read())
            self._next_read = self._clock() + self._interval
        return self._stopped


def stopping(context, *, refresh=False):
    # A Context belongs to this action/session. Keep no process-global task
    # handles, and capture its tasker rather than forming a Context cycle.
    query = getattr(context, '_maazmxy4_stop_query', None)
    if query is None:
        tasker = context.tasker
        query = StopQuery(lambda: tasker.stopping)
        context._maazmxy4_stop_query = query
    return query.refresh() if refresh else query()
