FROM twin-r0-local-base:659ea2913a7e
ARG REVISION
LABEL org.opencontainers.image.revision=$REVISION
LABEL plan-c.schema="0129b_model_retry_due" io.twin.offline-qualification="see-external-evidence"
LABEL twin.overlay.base="659ea2913a7ea203024ea58a992f24b0fcea6759" twin.overlay.files="service@$REVISION"
ENV BUILD_SHA=$REVISION
COPY service/ /app/service/
COPY offline-pytest/ /opt/offline-pytest/
COPY baseline-service/ /opt/r0-baseline/service/
COPY offline-node /usr/local/bin/node
COPY dashboard/ /work/
