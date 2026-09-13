# docker/ — Supporting files for Docker Compose

This directory contains supporting configuration files mounted into containers
at runtime.

## Structure

```
docker/
├── README.md
└── postgres/
    └── init/
        └── 01_timescaledb.sql   # Enables TimescaleDB extension on first start
```

## postgres/init/

Scripts placed here are executed automatically by the
`timescale/timescaledb:latest-pg15` container on **first start only**
(i.e. when the `pg_data` volume is empty).

They are mounted read-only at `/docker-entrypoint-initdb.d/` inside the
container and executed in alphabetical order.

| File | Purpose |
|---|---|
| `01_timescaledb.sql` | Creates `timescaledb` and `pg_stat_statements` extensions |

> **Note:** Alembic migrations (`alembic upgrade head`) are run separately
> after the container is healthy — typically as a one-shot `docker compose run`
> or a Kubernetes init-container, not inside `docker-entrypoint-initdb.d/`.

## Running migrations manually

```bash
# After `docker compose up -d postgres` is healthy:
docker compose run --rm api alembic upgrade head
```
