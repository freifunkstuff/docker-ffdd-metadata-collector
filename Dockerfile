FROM ghcr.io/freifunkstuff/ffdd-node:extending

RUN apk add --no-cache py3-yaml

ENV PYTHONPATH=/opt/metadata-collector

COPY requirements.txt /opt/metadata-collector/requirements.txt
COPY defaults.yaml /usr/local/share/freifunk/defaults-metadata-collector.yaml
COPY metadata_collector/ /opt/metadata-collector/metadata_collector/
COPY runit/metadata-collector/ /etc/service/metadata-collector/
COPY docker-entrypoint.d/50-metadata-collector /etc/docker-entrypoint.d/50-metadata-collector

RUN chmod +x /etc/service/metadata-collector/run \
	/etc/docker-entrypoint.d/50-metadata-collector
