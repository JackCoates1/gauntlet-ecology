# Gauntlet: Ecology

Gauntlet: Ecology is an evolutionary security challenge range: builders create challenge
versions while attacker and defender strategies compete in reproducible, scored matches.
This repository contains the system-of-record schema, the read-only query API, and the
supporting harness and infrastructure tracks.

It is a new project and is completely separate from the frozen
[`JackCoates1/the-gauntlet`](https://github.com/JackCoates1/the-gauntlet) hackathon entry.
Nothing here changes that submission.

## Local development

Start a disposable development Postgres instance:

```bash
docker compose -f docker-compose.dev.yml up -d
export DATABASE_URL=postgresql://gauntlet:gauntlet@localhost:5432/gauntlet_ecology
```

Apply the schema and its small example challenge:

```bash
for migration in schema/migrations/*.sql; do psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$migration"; done
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f schema/seed.sql
```

Run the read-only API:

```bash
python -m pip install -e '.[dev]'
uvicorn api.main:app --reload
```

The API uses `DATABASE_URL` and exposes `GET /generations`, `GET /generations/{id}`,
`GET /matches/{id}`, and `GET /leaderboard`. There are deliberately no write endpoints yet.

Run the integration tests against a fresh local schema (the database must be running):

```bash
export TEST_DATABASE_URL="$DATABASE_URL"
pytest
```

The collaborating tracks will document their runnable pieces in
[`harness/README.md`](harness/README.md) and [`infra/README.md`](infra/README.md) when those
directories land. This track does not modify their internals.
