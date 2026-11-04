FROM twin-repair:vault-system-main-20261105 AS contract
FROM twin-repair:vault-system-legacy-20261105
COPY --from=contract /app/service/db/schema.py /app/service/db/schema.py
COPY --from=contract /app/service/db/models.py /app/service/db/models.py
COPY --from=contract /app/service/db/onlyfans*models.py /app/service/db/
COPY --from=contract /app/service/db/migrations /app/service/db/migrations
COPY --from=contract /app/service/db/snapshots /app/service/db/snapshots
COPY --from=contract /app/service/onlyfans_runtime/launch_operations_models.py /app/service/onlyfans_runtime/launch_operations_models.py
COPY --from=contract /app/service/onlyfans_runtime/launch_ledger/models.py /app/service/onlyfans_runtime/launch_ledger/models.py
COPY --from=contract /app/service/onlyfans_runtime/history_policy.py /app/service/onlyfans_runtime/history_policy.py
COPY --from=contract /app/service/onlyfans_runtime/history_telemetry.py /app/service/onlyfans_runtime/history_telemetry.py
COPY --from=contract /app/service/onlyfans_runtime/launch_history_memory/recovery.py /app/service/onlyfans_runtime/launch_history_memory/recovery.py
COPY --from=contract /app/service/onlyfans_runtime/read_failures.py /app/service/onlyfans_runtime/read_failures.py
