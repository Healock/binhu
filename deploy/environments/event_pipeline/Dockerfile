ARG BASE_IMAGE
FROM ${BASE_IMAGE}
WORKDIR /opt/dev-pipeline
COPY event_pipeline /opt/dev-pipeline/event_pipeline
ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT []
CMD ["python", "-m", "event_pipeline.runtime", "relay"]
