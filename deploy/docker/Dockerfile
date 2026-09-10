FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CCAPPS_APP=/app \
    CCAPPS_CONFIGDIR=/app/config \
    CCAPPS_LOGDIR=/var/lib/ccapps/logs \
    CCAPPS_STATIC=/var/lib/ccapps/static \
    DJANGO_SETTINGS_MODULE=ccAPPSdb.settings

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libxerces-c3.2 libpq5 postgresql-client locales \
    && sed -i 's/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen \
    && locale-gen \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt gunicorn==23.0.0

# The image deliberately includes the locally-built scheduling engine.
COPY . /app
RUN mkdir -p /app/config /var/lib/ccapps/logs /var/lib/ccapps/static \
    && cp /app/djangosettings.py /app/config/djangosettings.py \
    && chmod +x /app/deploy/docker/entrypoint.sh

EXPOSE 8001
ENTRYPOINT ["/app/deploy/docker/entrypoint.sh"]
