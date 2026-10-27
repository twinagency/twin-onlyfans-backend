# Retained, tested dependency image from final artifact run 40541668140.
# Docker 29 manifest ID; producer config ID 7578467ec1c0f216983b9b1c763284d5b86cd8a74cb2a9e23d45a1bebca6ba2c.
ARG BASE_IMAGE=sha256:174d20ef97087371f3ace2c4c267ae83ff908218a996bb652c88614c7411fc61
FROM ${BASE_IMAGE}
ARG SOURCE_COMMIT
LABEL org.opencontainers.image.revision=${SOURCE_COMMIT} io.twin.release.schema="0097_release_controls"
RUN rm -rf /app/service
COPY service/ /app/service/
