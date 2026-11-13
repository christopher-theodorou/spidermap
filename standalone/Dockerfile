# SpiderMap standalone: its own web server and users, Zabbix read-only. Build from the standalone folder:
#   docker compose build        (see docs/STANDALONE.md)
# Only the program files are copied, by name - never settings, tokens or users, whatever else lies in the folder.
FROM php:8.3-apache-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-requests snmp ieee-data \
        iputils-ping iproute2 curl util-linux apache2-utils \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 spidermap && useradd -u 10001 -g spidermap -G www-data -d /var/lib/spidermap spidermap \
    && a2enmod auth_basic authn_file headers
COPY spidermap.py spidermap_*.py spidermap.html devices.example.json LICENSE /opt/spidermap/
COPY assets /opt/spidermap/assets
COPY standalone/apache.conf /etc/apache2/sites-available/000-default.conf
COPY standalone/entrypoint.sh /usr/local/bin/spidermap-entrypoint
RUN chmod 755 /usr/local/bin/spidermap-entrypoint && rm -f /var/www/html/index.html \
    && for f in zabbix.env devices.json secrets.json; do \
         if [ -e "/opt/spidermap/$f" ]; then echo "refused: $f in the image" >&2; exit 1; fi; done
ENV SPIDERMAP_DEPLOYMENT=standalone SPIDERMAP_ZABBIX_READONLY=1 SPIDERMAP_CONF=/etc/spidermap \
    SPIDERMAP_STATE=/var/lib/spidermap/state PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/spidermap
ENTRYPOINT ["/usr/local/bin/spidermap-entrypoint"]
CMD ["web"]
