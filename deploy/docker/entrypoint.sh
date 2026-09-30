#!/bin/sh
set -eu

db_host="${POSTGRES_HOST:-db}"
db_port="${POSTGRES_PORT:-5432}"
db_user="${POSTGRES_USER:-ccapps}"
db_password="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}"
export PGPASSWORD="$db_password"

mkdir -p "$CCAPPS_LOGDIR" "$CCAPPS_STATIC" "$CCAPPS_CONFIGDIR"

# Keep deploy-time secrets and host rules outside the image layers.
cat > "$CCAPPS_CONFIGDIR/localsettings.py" <<PY
import os
DEBUG = False
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
ALLOWED_HOSTS = [host.strip() for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost").split(",") if host.strip()]
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if origin.strip()]
PY

echo "Waiting for PostgreSQL at ${db_host}:${db_port}"
until pg_isready -h "$db_host" -p "$db_port" -U "$db_user" >/dev/null 2>&1; do sleep 1; done

if [ "${CCAPPS_BOOTSTRAP:-true}" = "true" ]; then
  python3 ccAPPSctl.py createdatabase --skip-if-exists
  python3 ccAPPSctl.py migrate --noinput
  python3 ccAPPSctl.py collectstatic --noinput --clear --verbosity=0
fi

# Planning web service for scenario1 listens on 8101 (gunicorn already owns 8001).
if [ "${CCAPPS_WEBSERVICE:-true}" = "true" ]; then
  loadplan=1 CCAPPS_DATABASE="${CCAPPS_WEBSERVICE_DATABASE:-scenario1}" \
    PYTHONPATH=/app python3 /app/ccAPPSdb/common/commands.py >> "$CCAPPS_LOGDIR/webservice.log" 2>&1 &
fi

exec gunicorn ccAPPSdb.wsgi:application \
  --bind "0.0.0.0:${PORT:-8001}" \
  --workers "${GUNICORN_WORKERS:-2}" \
  --timeout "${GUNICORN_TIMEOUT:-360}" \
  --access-logfile - --error-logfile -
