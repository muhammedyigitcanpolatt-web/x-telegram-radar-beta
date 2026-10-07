# Radar — supported Docker Compose deployment

The only supported topology is in `docker-compose.yml`. `docker-compose.prod.yml` includes that same file; it is not a separate Swarm or overlay deployment. Both entry points use the `shortmox-radar` project name. Do not combine the files with `-f ... -f ...`.

Local access: **http://127.0.0.1:8080** or **http://localhost:8080**. The gateway forwards `/` to frontend:3000 and `/api/v1/` and `/ws/` to fastapi_api:8005. Only the gateway's `127.0.0.1:8080` port is published. PostgreSQL, Redis, Neo4j, ClickHouse, frontend, and API have no published host ports. Data services use a bridge network with outbound access disabled; the backend also joins the application bridge network.

## Requirements and version contract

- Docker Engine/Desktop and Docker Compose **v2.20+**. Both `docker compose version` and `docker info` must work. The deployment script does not install Docker, start its service, or change the firewall.
- Backend: Python 3.12 and the backend maintainer's **requirements.lock**. The Dockerfile installs only from this lockfile; a missing lockfile stops the build. `requirements.txt` alone is insufficient.
- Frontend: Node 24 and Next **15.5.27** from `package-lock.json`. The build runs `npm ci` followed by `next build`. `RADAR_API_URL=http://fastapi_api:8005` is a server-side build argument. Optional `NEXT_PUBLIC_MAPBOX_TOKEN` is embedded in browser code at build time; use only a public Mapbox token there and never put secrets in build arguments.
- Data is stored in named volumes. PostgreSQL and ClickHouse schemas are initialized on a fresh installation. Do not automatically move or delete volumes from an older installation; see the upgrade section below.

## Prepare the local environment

Copy [`.env.example`](.env.example) to `.env` at the project root and keep `.env` out of Git. Do not overwrite an existing file wholesale. The required and optional settings are:

```dotenv
POSTGRES_USER=radar_app
POSTGRES_DB=shortmox_radar
POSTGRES_PASSWORD=
NEO4J_PASSWORD=
CLICKHOUSE_PASSWORD=
RADAR_SESSION_SECRET=
RADAR_INGEST_API_KEY=
RADAR_USERS_JSON=
RADAR_COOKIE_SECURE=false
RADAR_ALLOWED_ORIGINS=http://localhost:8080,http://127.0.0.1:8080
# Optional public Mapbox token; rebuild the frontend after changing it.
NEXT_PUBLIC_MAPBOX_TOKEN=
# Collection is opt-in; source credentials are needed only for enabled collectors.
COLLECTION_ENABLED=false
COLLECTION_MAX_RESULTS=25
COLLECTION_MAX_X_PAGES=5
COLLECTION_MAX_TELEGRAM_MESSAGES=100
X_COLLECTION_PROVIDER=scraping
X_SCRAPING_CREDENTIAL_SOURCE=env
X_PROXY_SOURCE=env
X_AUTH_TOKEN=
X_CT0=
X_PROXY_URL=
# Server-only token required when COLLECTION_ENABLED=true and X_COLLECTION_PROVIDER=scraping.
X_GRAPHQL_BEARER_TOKEN=
# X API v2 credential, used only when X_COLLECTION_PROVIDER=api.
X_BEARER_TOKEN=
TELEGRAM_API_ID=0
TELEGRAM_API_HASH=
TELEGRAM_COLLECTION_BOT_TOKEN=
TELEGRAM_SESSION_STRING=
TELEGRAM_TARGET_CHANNELS=
PUBLIC_CHAIN_LOOKUPS_ENABLED=false
SIEM_EXPORT_ENABLED=false
SIEM_WEBHOOK_URL=
```

Fill every required blank value before starting services. Generate each password independently with `openssl rand -hex 32`. Use only hexadecimal characters for the ClickHouse password because it appears in a URL. Surround the generated `RADAR_USERS_JSON` value with single quotes to prevent Compose from reinterpreting `$` signs in the PBKDF2 hash. Do not `source` this file from a shell. On Unix-like hosts, run `chmod 600 .env`; on Windows, restrict the file ACL to the deployment account and administrators.

If you use the map layer, set `NEXT_PUBLIC_MAPBOX_TOKEN` to a public Mapbox access token. It is passed to the frontend during `docker compose build` and is visible in the browser bundle. Run `bash deploy.sh up` to rebuild after changing it. An empty value leaves the regional list available without a map layer.

Generate a user hash in the same PBKDF2-SHA256 format as the backend's `hash_password()`. This command prompts without echoing the password and prints only the hash and user JSON:

```bash
python3 - <<'PY'
import getpass, hashlib, json, secrets
password = getpass.getpass('New admin password: ')
if len(password) < 12:
    raise SystemExit('Use a password of at least 12 characters.')
salt = secrets.token_hex(16)
digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()
encoded = 'pbkdf2_sha256$600000$' + salt + '$' + digest
print(json.dumps([{'username':'admin','password_hash':encoded,'role':'admin'}]))
PY
```

Before first use, put the output in `RADAR_USERS_JSON` in `.env`, surrounded by single quotes. Add separate `reader`, `analyst`, and `admin` accounts as needed. The ingest service key is never given to the browser; ingest clients use the separate server key in the `X-Radar-Ingest-Key` header. Administrators with Docker access can read container environments, so restrict access to `.env` and Docker. The application also supports `/run/secrets`, but this Compose deployment passes values through the runtime environment.

Restart the API after changing a user, role, or password hash in `RADAR_USERS_JSON`; existing user sessions become invalid. Browser cookie write requests require an exactly matching allowed `Origin` header. Server-key ingest clients without cookies are outside that browser check. Login attempts are limited per user and API-visible network peer in fixed 15-minute windows. With an external reverse proxy, configure its trusted rate limiting as well if clients need distinct IP limits.

## Check and start

From the directory where you cloned the repository, copy `.env.example` to `.env`, fill in the required values, and run the commands below. On Windows, use a shell that can run `bash` and Docker Compose. Quote paths that contain spaces.

```bash
bash deploy.sh check
bash deploy.sh up
bash deploy.sh status
```

To use another environment file: `RADAR_ENV_FILE=/absolute/path/.env bash deploy.sh up`. `up` builds first, waits with Compose `--wait`, then checks frontend and API HTTP access through the gateway. Failures return a nonzero status. `check` only validates configuration and starts no services. Plain `docker compose config` output may reveal secrets; use `config --quiet` for validation.

Ready and HTTP-reachable do not prove live-account collection, AI accuracy, or end-to-end archive transfer. Sign in and separately verify an authorized sample data flow.

Logs and stop:

```bash
bash deploy.sh logs
bash deploy.sh stop
```

The script does not automatically run `down`, delete volumes, install host packages or Tor, or change the firewall.

## Default and optional jobs

The default services are API, analyzer, frontend, gateway, PostgreSQL, Redis, Neo4j, and ClickHouse. The main ingest consumer runs `python -m app.workers.analyzer` as a separate process after the API becomes ready. Keep the analyzer at one replica: startup recovery of unfinished work assumes a single consumer.

The API `/health/ready` check verifies database initialization and Redis access. The analyzer health check reads the 30-second `radar:analyzer:state` marker in Redis; `RUNNING`, `PROCESSING`, and user-selected `PAUSED` are valid. This shows recent loop activity, not successful processing of a particular item. A long job can outlive the marker and report unhealthy; Compose does not automatically restart it for that condition. The gateway health check verifies HTTP access to the API, not the full analysis pipeline.

Celery queue workers use the **workers** profile; periodic jobs use **scheduler**. Without those profiles, Celery queue jobs and scheduled jobs do not run. Once the required service settings are in place:

```bash
COMPOSE_PROFILES=workers,scheduler bash deploy.sh up
```

Do not start the scheduler without workers, or jobs will wait in the queue. Use the same profiles for later status and stop commands, or set `COMPOSE_PROFILES=workers,scheduler` in `.env`. With collection disabled, jobs return `disabled` without connecting to sources. The admin **Operations** panel enqueues X query/campaign searches, Telegram channel sweeps, and link seeder jobs; sweep endpoints also require the admin session.

The admin kill switch first sets shared Redis state to `PAUSED`, clears queued jobs, and requests emergency `SIGKILL` of active Celery child processes. That signal does not run Python `finally` blocks. Retryable jobs, database transactions, and archive idempotency checks matter when work is interrupted. The API returns `PAUSED` only after confirming both Celery and analyzer are paused; inspect `PAUSE_REQUESTED` if full confirmation is unavailable. Use the admin `resume` action to restart work.

### Optional source collection

`COLLECTION_ENABLED` defaults to `false`. To opt in, set `COLLECTION_ENABLED=true` in `.env`; Compose passes it to the backend. For an incompletely configured source, the UI and job result show the names of missing environment variables without revealing their values.

- **X:** `X_COLLECTION_PROVIDER` defaults to `scraping`. This provider uses the existing X GraphQL client and paginated Latest results. Set the server-only `X_GRAPHQL_BEARER_TOKEN` as well as `X_AUTH_TOKEN` and `X_CT0` when using environment credentials. With `X_SCRAPING_CREDENTIAL_SOURCE=fleet`, an existing PostgreSQL `x_account_fleet` account is obtained through `FleetCommander.checkout_healthy_account()` and kept for one search chain; the worker needs a PostgreSQL pool. `X_GRAPHQL_BEARER_TOKEN` remains required in this mode. The default `X_PROXY_SOURCE=env` uses optional `X_PROXY_URL`; `fleet` selects a proxy from the existing `proxy_fleet` table; `none` disables proxy use. Environment mode does not automatically use existing account or proxy tables. HTTP 401/403/429 are explicit errors. In fleet mode, `FleetCommander.report_casualty()` marks the account under the existing rule, and the chain does not switch accounts midway.
  Select `X_COLLECTION_PROVIDER=api` explicitly to use [X API v2 Recent Search](https://docs.x.com/x-api/posts/search/quickstart/recent-search); only that mode requires `X_BEARER_TOKEN`. There is no automatic fallback between API and scraping. Provider cursors have separate Redis key spaces. The scraping provider stores the last completed ID as a high-water mark and stops at that mark on the next Latest scan; it rejects a cursor above it. `COLLECTION_MAX_RESULTS` ranges from 10–100 (default 25), and `COLLECTION_MAX_X_PAGES` ranges from 1–20 (default 5). When the page limit is reached, the job returns `partial` and can resume with that provider's continuation token.
- **Telegram:** Requires `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and an existing `TELEGRAM_SESSION_STRING` or `TELEGRAM_COLLECTION_BOT_TOKEN`. `TELEGRAM_TARGET_CHANNELS` is an explicit comma-separated allowlist, for example `@channel_a,@channel_b`. The collector does not join channels; it reads only channels already accessible to the configured account or bot. The first scan reads at most `COLLECTION_MAX_TELEGRAM_MESSAGES` recent messages per channel. Subsequent five-minute scans continue from the last ID.
- **Telegram link seeders:** The X link scan uses the same `X_COLLECTION_PROVIDER` and X credentials as source collection. The OSINT seeder scans only the public-source archive hardcoded in the code and adds discovered invite links to the analysis pool; it does not join groups. With the scheduler profile, these seeders run every five and four hours respectively.
X `auth_token`/`ct0`, `X_GRAPHQL_BEARER_TOKEN`, credentials embedded in proxy URLs, the X API bearer token, Telegram session strings, bot tokens, and API hashes are secrets. This Compose example passes them as runtime environment values and does not define Docker secret mounts for collector credentials. Restrict `.env` permissions as described above. `persona_warming_cycle` and legacy `telegram_stealth.py` flows remain disabled. The private GraphQL endpoint and operation ID used for scraping may change. Live X accounts and external requests have not verified this provider, so operation is not guaranteed. Fixture tests cover recorded response formats and client behavior only.
`SIEM_EXPORT_ENABLED` and `PUBLIC_CHAIN_LOOKUPS_ENABLED` also pass from `.env` to the backend and default to `false`. Enable SIEM only with an HTTPS `SIEM_WEBHOOK_URL` and deliberate export of approved reports. Balance checks make outbound requests when `PUBLIC_CHAIN_LOOKUPS_ENABLED=true`.
This package does not provision an Ollama/model server, external SIEM receiver, Telegram credentials, Tor, or an MTProto proxy. The default Ollama URL points to the backend container's loopback address, where no model service is installed; model-based enrichment is unavailable in that state. If an authorized Ollama server and the required models are available, set a reachable `OLLAMA_URL` in `.env`. API readiness does not mean an AI model is ready.
Image enrichment downloads only HTTP/80 or HTTPS/443 raster images that resolve to public IP addresses. Redirects are revalidated. Responses are capped at 5 MiB and total downloads at 15 seconds. Private-network addresses, other ports, unsupported types, and failed downloads produce a safe default image-analysis result. This applies particularly to untrusted `media_url` values in ingest records.
The manually callable COMINT job has no real audio transcription integration; it returns `unavailable` without storing sample transcripts or audio fingerprints. The experimental IMINT job reads local images up to 5 MiB and does not write a `(0,0)` location when the model's proposed region is absent from the reference matrix. Registration of these jobs does not establish validated production audio or geolocation analysis.
`tasks.run_predictive_anomalies` is absent from the Beat schedule. Its old Z-score-derived percentage is not calibrated as a real threat probability, so even manual calls now return `unavailable` without a ClickHouse query or PostgreSQL alert record. Old `predictive_early_warnings` rows are retained but are not displayed as active alerts without validation.
## HTTPS access for a public beta
Use the same Compose stack. Install a certificate-backed reverse proxy on the host and forward it to the loopback gateway; **do not expose port 8080 directly to the internet**. Preserve the `Host`, `X-Forwarded-Proto: https`, and WebSocket Upgrade/Connection headers. Set `.env` for your actual domain:
```dotenv
RADAR_COOKIE_SECURE=true
RADAR_ALLOWED_ORIGINS=https://radar.example.com
```
The domain is illustrative; DNS and certificates are not configured automatically. Sign in only through the actual HTTPS address in this mode. `RADAR_COOKIE_SECURE=false` is for loopback HTTP only. The extension's current host permissions cover only loopback dashboard addresses; this package does not grant remote HTTPS extension access.

## Upgrades and existing data

This Compose project and its volume names do not automatically match old `cti_prod_*` containers or older Compose project volumes. If an older installation exists, inventory its projects and volumes and back it up first. A newly empty database is not a migration. Changing a password in `.env` does not change a user password stored in an existing PostgreSQL or Neo4j volume.

Review backend PostgreSQL migrations and changes under `app/intel/clickhouse_migrations/` alongside the release notes. ClickHouse entrypoint SQL runs only when its data directory is first initialized; an existing installation needs an explicit migration. Do not use `down -v` to move data. A rollback must account for schema and backup compatibility, not just an earlier application image.

PostgreSQL `004_coordinated_signal_kind.sql` adds a `signal_kind` column and partial unique index for new anomaly records. It does not classify or remove old rows. Confirm successful migration before starting the new analyzer. It was tested in an isolated Windows PostgreSQL database, not applied to existing user data.

The archiver rechecks the PostgreSQL row version before deleting a row copied to ClickHouse. If enrichment changes the row during copying, the batch rolls back, the hot row remains, and a later archive attempt copies the updated content. The archiver and heavy AI job share a PostgreSQL advisory lock. A heavy AI job starting after archival verifies report and source identity and text, then inserts a higher-version cold row; it reports success only after verifying the `FINAL` readback. Missing, multiple, or legacy cold rows with `tweet_id=''` are not matched automatically.

For the **versioned cold-table migration** on existing data, keep this order:

1. Stop the archiver, heavy AI workers, and other writers to the same cold table. Back up PostgreSQL and ClickHouse and verify that the backups can be restored. This migration has not been run on real data as part of this project work.
2. Apply PostgreSQL `005_cold_write_version.sql` and verify `public.cold_write_version_seq` and the migration marker. Do not reset the sequence later; gaps after failed writes and rollbacks are normal.
3. For an existing ClickHouse table with the old schema, apply `001_replacing_merge_tree.sql`, then `002_versioned_replacing_merge_tree.sql` in a maintenance window. `002` copies visible old rows with version 1, compares row counts and content summaries, swaps the table atomically, and keeps the old table as `historical_threat_signals_pre_version`. On a fresh empty volume, `clickhouse_init.sql` already creates `ReplacingMergeTree(version)`; do not run `002` there. Check `SHOW CREATE TABLE` for `ReplacingMergeTree(version)` and verify expected `FINAL` row counts. If a partial or repeat migration is rejected, recover manually from backups and retained tables.
4. Start new API and Celery images only after both database transitions are verified. The new archive writer rejects writes if PostgreSQL lacks the sequence or ClickHouse lacks the version column. Check data compatibility before pointing an older unversioned writer at this table.

Versioning prevents an older archive write that finishes after a timeout from overwriting newer enrichment. All writers must use this ordering. The global archive lock can delay heavy AI and new CTI writes during large batches.

### Archived source identity migration

PostgreSQL `006_archived_cti_source_identities.sql` creates a CTI source identity ledger that remains after archival. The new archiver deletes only rows whose ClickHouse write was confirmed and whose PostgreSQL version still matches; it adds the source identity of each deleted row to the ledger in the **same PostgreSQL transaction**. New analysis and direct CTI writes skip exact replays found there and reject reuse of the same source ID with a different platform, channel, message, or text. Analysis writes the CTI record and pending report in one transaction. Analysis beginning concurrently with archival may emit counters or heuristic/semantic alerts before its final locked check; that narrow race does not guarantee exactly-once behavior for every side effect.

Applying `006` does not automatically add old ClickHouse rows to the ledger. For an existing installation, stop analysis, archival, and cold-enrichment writers and back up PostgreSQL and ClickHouse. Confirm that `005` and the versioned ClickHouse migration have completed. Apply `006`, then check `schema_migrations` and the table. Run `python -m app.cron.backfill_archived_cti_identities` **first in its default dry-run mode**, using the same PostgreSQL/ClickHouse settings. The tool independently compares `FINAL` row counts before and after scanning and stops on an incomplete stream or changed count. All ClickHouse writers must remain stopped during this check. Inspect `cold_rows`, `already_present`, `would_insert`, and rejected rows. Only with consistent results, rerun with `--apply`, then repeat the dry run. Finally restart writers with the new code. No backfill or migration on real data has been performed for this project work.

The tool validates ClickHouse `FINAL` visible rows and source identity inside `payload_json`. It stops for empty or conflicting sources, duplicate IDs, hot-record collisions, or ledger mismatches. It also requires the next PostgreSQL `signal_id` for a hot CTI record to exceed the maximum hot, ledger, and cold ID. If a restored sequence is behind, the job stops; advance it safely during maintenance and rerun the dry run. Otherwise a new hot record could reuse an old ClickHouse `id` and hide its archived row from `FINAL`. The tool does not infer identities from legacy rows with `tweet_id=''`; reconcile them manually against backups and source records before declaring migration complete. For backfilled rows, `archived_at` is ledger insertion time, not historical archive time. `--apply` is not an automatic deletion-based rollback; verify backup and restore plans before running it on real data.

## Optional Linux systemd wrapper

`shortmox-enterprise.service` no longer starts a host virtual environment or Uvicorn. It calls the same `deploy.sh up/stop` path. Edit `WorkingDirectory`, `ExecStart`, and `ExecStop` to match the actual project path before enabling it. Installation is not automatic. Container restart policies are set in Compose.

## Verification status and beta limits

In local validation on 3 October 2026, API and frontend images built in an isolated Windows Docker Compose project. Eight default services became healthy; PostgreSQL migrations 001–005 and pgvector were verified. Synthetic ingest, ClickHouse archival, WebSocket/Redis fan-out, and Celery pause paths were checked with local services. Legacy schema migration was checked only in disposable databases containing synthetic rows. A concurrent update left the hot row in place until a later archive pass copied its current content to ClickHouse. A synthetic archive → cold enrichment → delayed old write check preserved the newest `FINAL` result.

Subsequent disposable PostgreSQL 16 and ClickHouse 25.8 checks covered the `006` identity ledger, archive compare-and-delete, rollback during concurrent update, exact replay suppression, direct CTI write protection, and `FINAL` readback. Backfill dry-run and `--apply` SQL were exercised only with synthetic ClickHouse responses and a disposable PostgreSQL database. They have not run on a real archive. The overview card counts anomaly detections from `coordinated_signals`; different detection kinds for one source can count separately.

In local follow-up on 7 October 2026, 229 Python tests passed with one Starlette deprecation warning; Python compilation, frontend typecheck/lint/production build, and 13 extension tests passed. The backfill tool compares independent `FINAL` row counts before and after scanning, rejects duplicate JSON keys, and stops before writes if the hot/ledger/cold `signal_id` sequence is behind. Analyzer, direct CTI writer, and archiver verify archived, hot, and raw identities under a shared lock. Review evidence after archival uses only matching raw records or report evidence. An isolated PostgreSQL 16 and ClickHouse 25.8 smoke run used synthetic data to cover the archive race, current `FINAL` payload, ledger entry, exact replay, report rollback, backfill dry run/apply, and lagging sequence rejection.

Live accounts, real user data, and production deployment have not been tested. Before exposing a beta dashboard, configure external TLS termination and trusted rate limiting at the reverse proxy, use unique credentials, and verify the deployed image and data flow with authorized sample data.
