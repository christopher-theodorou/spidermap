# SpiderMap standalone: its own web server and users, Zabbix read-only. Build from the repository root:
#   docker compose -f standalone/compose.yaml build        (see docs/STANDALONE.md)
FROM php:8.3-apache-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-requests snmp ieee-data \
        iputils-ping iproute2 curl util-linux apache2-utils \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 spidermap && useradd -u 10001 -g spidermap -G www-data -d /var/lib/spidermap spidermap \
    && a2enmod auth_basic authn_file headers
COPY . /opt/spidermap
COPY standalone/apache.conf /etc/apache2/sites-available/000-default.conf
COPY standalone/entrypoint.sh /usr/local/bin/spidermap-entrypoint
RUN chmod 755 /usr/local/bin/spidermap-entrypoint && rm -f /var/www/html/index.html \
    && rm -rf /opt/spidermap/standalone/auth /opt/spidermap/.git
ENV SPIDERMAP_DEPLOYMENT=standalone SPIDERMAP_ZABBIX_READONLY=1 SPIDERMAP_CONF=/etc/spidermap \
    SPIDERMAP_STATE=/var/lib/spidermap/state PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/spidermap
ENTRYPOINT ["/usr/local/bin/spidermap-entrypoint"]
CMD ["web"]
