"""Event-bus seam for aux input streams.

STUB — never couples into the acq/encode hot loops. Real timestamped aux-event
sink lands in the Event-Bus round.
"""

import logging
import time

from .context import Context


class EventBus:
    def __init__(self, context: Context) -> None:
        self.context = context
        self.logger = logging.getLogger("frameforge.eventbus")

    def run(self) -> None:
        self.logger.info("[stub] event-bus seam starting (no MVP sink)")

        while not self.context.drain.is_set():
            time.sleep(5.0)

        self.logger.info("[stub] event-bus seam stopping")
