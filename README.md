# X + Telegram Radar

**Public beta — v0.1.0-beta.1.** A self-hosted interface for optional collection, analysis, analyst review, and alerts from X and Telegram sources. The stack uses FastAPI, Redis, PostgreSQL/pgvector, Neo4j, ClickHouse, and Next.js. Source collection, SIEM export, and public blockchain lookups are disabled by default.

This beta has been checked with local fixtures and isolated Docker services. Live social accounts, real user data, production migration, and a public deployment have not been verified. Analysis results require human review; they are not confirmed threat findings.

## Supported setup

The supported deployment uses the root [`docker-compose.yml`](docker-compose.yml) and [`deploy.sh`](deploy.sh). [`docker-compose.prod.yml`](docker-compose.prod.yml) is a compatibility entry point for the same stack; do not combine both Compose files with `-f`. Copy [`.env.example`](.env.example) to `.env`, then complete the required secrets, user password hash, and Docker/Compose prerequisites in [DEPLOY.md](DEPLOY.md). Follow its HTTPS section before exposing the dashboard to the internet.

```bash
bash deploy.sh check
bash deploy.sh up
bash deploy.sh status
```

The gateway binds only to loopback at `http://127.0.0.1:8080`. API and frontend host ports are not published. `check` validates the Compose configuration; `up` builds services and waits for HTTP health checks. A healthy stack does not prove that live X/Telegram collection or model analysis works end to end.

## Data flow

```text
X / Telegram (opt-in) → authenticated ingest → Redis queue
                                         → analyzer → PostgreSQL/pgvector
                                                    → alert event → WebSocket → dashboard
                                                                            → Chrome extension
```

Neo4j relationship analysis and the ClickHouse cold archive are separate components. Celery jobs require the `workers` profile, and scheduled jobs additionally require `scheduler`. Model-based enrichment requires an accessible, authorized model server. See [DEPLOY.md](DEPLOY.md) for details.

## Access and browser extension

The dashboard uses user sessions with `reader`, `analyst`, and `admin` roles. Ingest uses a separate server-only `X-Radar-Ingest-Key`. WebSocket connections require a user session and an allowed origin. Set secure cookies to `false` for local HTTP and `true` for HTTPS access, as described in [DEPLOY.md](DEPLOY.md).

The supported Chrome extension is in [`extension/`](extension/). [`chrome_extension/`](chrome_extension/) is an inactive legacy version. See [extension/README.md](extension/README.md) for installation and its current localhost limitation.

## Development and verification

- Backend dependencies are pinned with hashes in `requirements.lock`; the container installs from that file.
- See [frontend/README.md](frontend/README.md) for frontend setup.
- Local fixture tests: `./.venv/bin/python -m pytest tests -q` and `node --test extension/tests/*.test.cjs` when their dependencies are installed.
- See [DEPLOY.md](DEPLOY.md) for deployment checks, migration steps, and verification limits.

## License

MIT. See [LICENSE](LICENSE).
