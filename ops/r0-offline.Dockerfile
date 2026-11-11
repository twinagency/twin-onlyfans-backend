FROM twin-r0-local-base:659ea2913a7e
ARG REVISION
LABEL org.opencontainers.image.revision=$REVISION
ENV BUILD_SHA=$REVISION
COPY service/ /app/service/
COPY offline-pytest/ /opt/offline-pytest/
COPY baseline-service/ /opt/r0-baseline/service/
