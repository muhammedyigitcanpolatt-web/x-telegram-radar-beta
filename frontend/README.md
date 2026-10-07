# Radar frontend

The application lives in `app/`, shared components in `components/`, and client contracts in `lib/` and `types/`. `/review` opens lexicon and report review under the same session and role controls.

## Setup and verification

Node.js 20.9 or newer is required. This work was verified with Node.js 24.21.0.

```sh
npm ci
npm run typecheck
npm run lint
npm run build
npm run test:proxy
npm start
```

`test:proxy` starts a real production Next server on 127.0.0.1:3105 and a fixture backend containing test data only on 127.0.0.1:8005. If port 8005 is occupied, it fails without touching the existing backend. The test requires a build prepared with the default `RADAR_API_URL`. It does not use real Redis/PostgreSQL or social accounts.

## Backend and sessions

- Set the backend origin reachable by the Next server with `RADAR_API_URL`; its default is `http://127.0.0.1:8005`. Use the service address in a container. This value becomes a rewrite rule at build time, so rebuild after changing it.
- Browser HTTP and WebSocket requests go to the browser's own origin. Next forwards `/api/v1/*` and `/ws/*` to the backend. No service key is added to URLs.
- Backend `RADAR_ALLOWED_ORIGINS` must contain the browser's actual origin. For direct local Next access, include `http://localhost:3000` or `http://127.0.0.1:3000`. For the gateway, include its origin.
- Local HTTP testing requires backend `RADAR_COOKIE_SECURE=false`. Use HTTPS and `RADAR_COOKIE_SECURE=true` for public access. JavaScript neither reads nor stores the HttpOnly session cookie.
- `reader` can view data; `analyst` and `admin` can make review decisions; only `admin` sees operations. The backend is the final authority for these permissions.
- Real users and passwords are configured in the backend, not the frontend.
- If the geographic API supplies only region names, the UI shows a region list rather than inventing coordinates. A map needs real latitude/longitude data and optional `NEXT_PUBLIC_MAPBOX_TOKEN`. Set the token in the build environment for direct Next builds or in root `.env` for the supported Compose deployment, then rebuild. The token is visible in browser code; use only a public Mapbox access token.

## Dependencies

The application pins Next.js 15.5.27. Review upstream security releases before each deployment. PostCSS is pinned to 8.5.28 with an override that also covers Next's older transitive version. `package-lock.json` supports reproducible installation.

Tailwind 4.3.3 and `@tailwindcss/postcss` 4.3.3 are used. The former Tailwind 3 configuration was moved to CSS `@theme` variables; the production build and proxy test passed.

On 3 October 2026, `npm audit --omit=dev` reported zero findings. The full audit still reported **5 high** findings in the development-only `eslint-config-next` → `@next/eslint-plugin-next` → `fast-glob` → `micromatch` → `braces` chain. The [braces advisory](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm) did not list a fixed version at that time. Do not pass untrusted input to build or lint tooling.
