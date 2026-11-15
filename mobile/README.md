# Agent Ops mobile app

Expo / React Native client for the coordinator with the same features and
owner/member/viewer rules as the web app (epic #83). It signs in with a
per-device token (`POST /api/v1/auth/tokens`) kept in the platform keychain via
`expo-secure-store`. Signing out revokes the token on the server; you can also
revoke devices from **Account** in either app.

| Tab | What it does |
| --- | --- |
| Tasks | Board by status or sortable list, search, project/agent/status/ready filters, agent strip, task detail with Markdown and a paged activity timeline |
| Planning | Projects, new project, board cards (labels, checklist, instructions), run/queue, queue order and completion, project settings, play/pause/stop |
| Sessions | Session library (pinned and recent, kept on the device), workers and their health, new sessions, live chat with prompts, interrupt, permission and question replies |
| Activity | Recent worker commands and task activity |
| More | Approvals, Account (password, devices), and for owners Users & invites and agent Configuration |

Live updates come from `/ws/dashboard/` with a one-time ticket
(`POST /api/v1/realtime/ticket`); the device token never goes over the socket.

## Run it

```sh
# Terminal 1: the API (from the repository root)
uv run uvicorn config.asgi:application --host 0.0.0.0 --port 8000

# Terminal 2: the app (iOS simulator)
npm ci --prefix mobile
EXPO_PUBLIC_API_URL=http://localhost:8000 npm run ios --prefix mobile
```

### Android emulator

The emulator reaches the host at `10.0.2.2`, so allow that host and point the
app at it:

```sh
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,10.0.2.2 uv run uvicorn config.asgi:application --host 0.0.0.0 --port 8000
EXPO_PUBLIC_API_URL=http://10.0.2.2:8000 npm run android --prefix mobile
```

Expo Go (SDK 58) runs the app without a native build. The sign-in screen lets
you change the server address at any time. Use an account created with
`create_owner` or an invite link.

## Build an APK to sideload

Android's native project is generated, not committed (`android/` is ignored).
With JDK 17 (`brew install openjdk@17`) and the Android SDK:

```sh
cd mobile
CI=1 npx expo prebuild --platform android --no-install
cd android
JAVA_HOME=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home \
ANDROID_HOME=$HOME/Library/Android/sdk \
EXPO_PUBLIC_API_URL=https://atm.aaron-henriquez.com NODE_ENV=production \
./gradlew assembleRelease -PreactNativeArchitectures=arm64-v8a
```

The APK lands in `android/app/build/outputs/apk/release/app-release.apk`.
`EXPO_PUBLIC_API_URL` becomes the sign-in screen's default server. The release
build is signed with the Android debug key: fine for installing directly, not
for the Play Store. Revert the `android`/`ios` scripts prebuild rewrites in
`package.json` before committing.

## Code layout

- `src/app/` — Expo Router screens: `(tabs)/` holds one Stack per tab.
- `src/api/` — the typed client and `schema.d.ts` (generated, see below);
  `types.ts` covers endpoints the OpenAPI schema leaves untyped.
- `src/live/` — the app-wide live connection and focused-session streaming.
- `src/auth/` — session (token, server) and role access.
- `src/tasks/`, `src/sessions/`, `src/planning/` — data hooks and parts per area.
- `src/ui/` — shared kit, Markdown renderer, formatting and theme.

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
