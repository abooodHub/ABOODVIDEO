FROM mwader/static-ffmpeg:9.0.2-1@sha256:f5d100184884eb3e82b0eb14bb5ebf98da99826ac824989720297fb9ef21c822 AS media

FROM python:3.12-alpine3.24 AS dependencies
RUN apk add --no-cache build-base libffi-dev openssl-dev postgresql-dev
COPY backend/requirements.txt /requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r /requirements.txt

FROM python:3.12-alpine3.24

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install minimal required OS packages
RUN apk upgrade --no-cache \
    && apk add --no-cache bash curl procps libpq libstdc++ ca-certificates fontconfig font-dejavu
COPY --from=media /ffmpeg /ffprobe /usr/local/bin/
COPY --from=media /versions.json /usr/share/doc/ffmpeg/versions.json
COPY --from=media /doc/ /usr/share/doc/ffmpeg/
COPY backend/vendor/ffmpeg/ /usr/share/doc/ffmpeg/
COPY --from=dependencies /install /usr/local
# Package installation is confined to the build stage. Remove the runtime
# installer and its bundled dependency copies from the shipped image.
RUN python -m pip uninstall --yes pip

# Install python dependencies
COPY backend/requirements.txt .

# Only runtime storage is writable by the application. Source stays root-owned.
RUN addgroup -g 10001 -S aboodvideo \
    && adduser -u 10001 -G aboodvideo -S -D -H -s /sbin/nologin aboodvideo \
    && mkdir -p /app/storage/uploads /app/storage/videos /app/storage/temp /app/logs \
    && chown -R 10001:10001 /app/storage /app/logs

# Copy application code and tests
COPY backend /app
COPY tests /app/tests
COPY scripts/setup_test_owner.py /app/scripts/setup_test_owner.py
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER 10001:10001

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
