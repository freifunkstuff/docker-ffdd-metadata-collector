FROM ghcr.io/freifunkstuff/ffdd-node:v1.0.0

RUN apk add --no-cache py3-yaml

ENV PYTHONPATH=/opt/metadata-collector

COPY defaults.yaml /usr/local/share/freifunk/defaults-metadata-collector.yaml
COPY metadata_collector/ /opt/metadata-collector/metadata_collector/
COPY ui/extensions/ /usr/local/share/freifunk/ui/extensions/
COPY runit/metadata-collector/ /etc/service/metadata-collector/
COPY docker-entrypoint.d/50-metadata-collector /etc/docker-entrypoint.d/50-metadata-collector

RUN chmod +x /etc/service/metadata-collector/run \
	/etc/docker-entrypoint.d/50-metadata-collector
