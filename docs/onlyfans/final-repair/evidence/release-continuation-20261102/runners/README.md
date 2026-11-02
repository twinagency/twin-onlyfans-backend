# Actual qualification runner snapshots

These are evidence of the task-owned execution, not new production tools.

* `service_sweep.py`: final 285-module run, three independent ordinary module processes, sequential timing/browser/native-service checks, source hashes and owned PostgreSQL shutdown.
* `capacity.py`: four final declared workloads, after the service sweep, with a fresh owned cluster.
* `managed_proxy.py`: supplemental 36-check suite with its own cluster in the same execution session.

Paths, ports and the retained test-only UID shim are specific to the captured environment. Adapt them only for a fresh disposable test environment. Never point them at production.

A runner/benchmark process exit zero means collection finished. Individual module failures and timing/correctness results remain authoritative; it does not mean release acceptance passed. The complete results retain the two missing real Twin prerequisites and Linux `cmd` failure, with separate native Windows proof.
