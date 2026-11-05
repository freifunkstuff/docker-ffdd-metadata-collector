FROM ghcr.io/freifunkstuff/ffdd-node:v1.0.1 AS base

FROM base AS tests
ENV PYTHONPATH=/opt/metadata-collector
COPY metadata_collector/ /opt/metadata-collector/metadata_collector/
COPY tests/ /opt/metadata-collector/tests/
COPY scripts/migrate-victoriametrics.py /opt/metadata-collector/scripts/migrate-victoriametrics.py
RUN cd /opt/metadata-collector \
 && python3 -m unittest discover -v -s tests -t . \
 && printf 'Collector unit and Node producer/consumer tests passed\n' > /tmp/collector-tests-passed

FROM base AS final
ENV PYTHONPATH=/opt/metadata-collector
COPY --from=tests /opt/metadata-collector/metadata_collector/ /opt/metadata-collector/metadata_collector/
COPY --from=tests /tmp/collector-tests-passed /usr/local/share/freifunk/collector-tests-passed
COPY defaults.yaml /usr/local/share/freifunk/defaults-metadata-collector.yaml
COPY ui/extensions/ /usr/local/share/freifunk/ui/extensions/
COPY runit/metadata-collector/ /etc/service/metadata-collector/
COPY docker-entrypoint.d/50-metadata-collector /etc/docker-entrypoint.d/50-metadata-collector

RUN chmod +x /etc/service/metadata-collector/run \
	/etc/docker-entrypoint.d/50-metadata-collector
