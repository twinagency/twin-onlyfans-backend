"""Read-only financial polling independent of chat/AI and content processing."""
import threading
from .ledger import process_ledger_pages

class LedgerWorker(threading.Thread):
    def __init__(self, store, tenant, stop, *, observe=None, process=None):
        super().__init__(name="onlyfans-ledger", daemon=True)
        self.store, self.tenant, self.stop = store, tenant, stop
        self.observe, self.process = observe, process or process_ledger_pages

    def run(self):
        while not self.stop.is_set():
            try:
                # Durable scan leases serialize each lane. The shared provider
                # budget reserves intake capacity and honours provider backoff.
                self.process(self.store, self.tenant, limit=1, stop=self.stop)
                if self.stop.is_set():
                    break
                self.process(self.store, self.tenant, limit=1, stop=self.stop, history=True)
            except Exception:
                if self.observe:
                    self.observe({"schemaVersion": "of-worker.v1", "state": "ledger_unavailable"})
            self.stop.wait(2)
