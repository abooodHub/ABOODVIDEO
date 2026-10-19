FROM postgres:16-alpine
RUN apk upgrade --no-cache \
    && rm -f /usr/local/bin/gosu
# Official PGDATA is owned by postgres. New named volumes inherit that ownership;
# no root helper is needed and the server never starts as root.
USER postgres
