# Production Docker Deployment

This bundle deploys PostgreSQL and ccAPPS with the CCPL and FAS code in this
working tree. The scheduler library is copied from `bin/libccAPPS.so`.

## First deployment

```bash
cd deploy/docker
cp .env.example .env
# Edit .env: set unique POSTGRES_PASSWORD and DJANGO_SECRET_KEY.
docker compose --env-file .env -f compose.yml up -d --build
docker compose --env-file .env -f compose.yml logs -f app
```

Open `http://SERVER:8001/data/login/` after the application health check is up.
The first start creates all scenario databases and runs migrations.

## Existing data migration

Stop the source system before taking a PostgreSQL dump. Restore its `default`
and all required `scenarioN` databases into the `db` service before starting
the app. Database volumes are the source of truth; rebuilding the app image
doesn't remove data.

For a restored production database, set `CCAPPS_BOOTSTRAP=false` in `.env`
for the first app start. After validating the restore, set it back to `true`
for normal upgrades so database migrations are applied automatically.

## Upgrade

```bash
cd deploy/docker
docker compose --env-file .env -f compose.yml up -d --build
```

Take a PostgreSQL backup before upgrades. Do not publish port 8001 directly to
the Internet; put Nginx or another TLS reverse proxy in front of it.
