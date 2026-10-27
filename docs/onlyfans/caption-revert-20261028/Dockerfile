# The immutable current production handover image; no dependency/schema upgrade.
FROM sha256:ab3bfc200c0e93a8fe5ebfd1f93790efb5f4e96981af7999cb33b314e8e42207
ARG SOURCE_COMMIT
ARG SOURCE_DATE_EPOCH
LABEL org.opencontainers.image.revision=$SOURCE_COMMIT
LABEL io.twin.release.kind="requested-caption-revert"
LABEL io.twin.release.base="sha256:ab3bfc200c0e93a8fe5ebfd1f93790efb5f4e96981af7999cb33b314e8e42207"
LABEL io.twin.release.schema="0092_ledger_history"
COPY service/automations/content_prompts.py /app/service/automations/content_prompts.py
COPY service/automations/content_resolver.py /app/service/automations/content_resolver.py
COPY service/automations/pack_claim.py /app/service/automations/pack_claim.py
COPY service/automations/pack_sender.py /app/service/automations/pack_sender.py
COPY service/tests/test_fastt_requested_caption.py /app/service/tests/test_fastt_requested_caption.py
