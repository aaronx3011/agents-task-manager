# Agent Ops mobile app

Expo / React Native client for the coordinator (#54). It signs in with a
per-device token (`POST /api/v1/auth/tokens`), keeps it in the platform
keychain via `expo-secure-store`, and shows a read-only, paginated task list
with task details. Signing out revokes the token on the server; owners and the
user can also revoke it from **Account → Mobile devices** in the web app.

## Run in the iOS simulator

```sh
# Terminal 1: the API (from the repository root)
uv run uvicorn config.asgi:application --port 8000

# Terminal 2: the app
npm ci --prefix mobile
EXPO_PUBLIC_API_URL=http://localhost:8000 npm run ios --prefix mobile
```

The sign-in screen lets you change the server address; the simulator reaches
the host at `localhost`. Use an account created with `create_owner` or an
invite link.

## API types

`src/api/schema.d.ts` is generated from `openapi/schema.json` together with the
web client's types: run `npm run api:generate --prefix frontend` (CI fails when
either copy drifts). Do not edit it by hand.

## Checks

```sh
npm run typecheck --prefix mobile
npm run lint --prefix mobile
npm run format:check --prefix mobile
```
