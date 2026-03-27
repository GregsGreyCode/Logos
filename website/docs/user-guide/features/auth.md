---
sidebar_position: 20
title: "Auth & RBAC"
description: "User authentication, role-based access control, user management, and audit logging for the Hermes dashboard and HTTP API"
---

# Auth & RBAC

Hermes has a built-in authentication and role-based access control (RBAC) system for its HTTP API and dashboard. This controls who can log into the dashboard, spawn agent instances, delete instances, and perform administrative actions.

## Overview

The auth system uses:

- **Email + password** login (Argon2id hashing)
- **httpOnly JWT cookies** for sessions (15-minute access tokens, 7-day rotating refresh tokens)
- **CSRF double-submit** protection on all mutations
- **SQLite** for user and token storage (`~/.logos/auth.db`)
- **4 fixed roles** mapping to permission sets

All routes except `/health` and `/login` require an authenticated session. Unauthenticated browser requests are redirected to `/login` automatically.

## Roles

| Role | Description |
|------|-------------|
| `admin` | Full access — user management, platform settings, all instance ops |
| `operator` | Spawn/delete instances, toggle providers, view audit logs |
| `user` | Spawn instances with `user_accessible` souls only, view dashboard |
| `viewer` | Read-only access to instances and dashboard |

## Permissions

| Permission | admin | operator | user | viewer |
|-----------|:-----:|:--------:|:----:|:------:|
| `spawn_instance` | ✅ | ✅ | — | — |
| `spawn_instance_restricted` | — | — | ✅ | — |
| `delete_instance` | ✅ | ✅ | — | — |
| `view_instances` | ✅ | ✅ | ✅ | ✅ |
| `override_toolsets` | ✅ | ✅ | — | — |
| `manage_users` | ✅ | — | — | — |
| `manage_souls` | ✅ | — | — | — |
| `view_audit_logs` | ✅ | ✅ | — | — |
| `manage_settings` | ✅ | — | — | — |
| `manage_platform` | ✅ | — | — | — |
| `promote_canary` | ✅ | — | — | — |

`spawn_instance_restricted` allows spawning only souls with `user_accessible: true` in their soul manifest.

## First Boot — Admin Seed

On first start, Hermes seeds an admin account from environment variables:

```bash
HERMES_ADMIN_EMAIL=admin@example.com
HERMES_ADMIN_PASSWORD=your-strong-password
HERMES_ADMIN_NAME=Admin        # display name (optional, defaults to "Admin")
HERMES_JWT_SECRET=<32-byte hex> # generate: openssl rand -hex 32
```

If the email already exists in the database, the seed is skipped. The seed only runs once.

On Kubernetes, set these in the `hermes-secret` Secret:

```bash
kubectl patch secret hermes-secret -n hermes --type='json' \
  -p='[{"op":"add","path":"/data/HERMES_ADMIN_EMAIL","value":"'"$(echo -n 'you@example.com' | base64 -w0)"'"}]'

kubectl patch secret hermes-secret -n hermes --type='json' \
  -p='[{"op":"add","path":"/data/HERMES_ADMIN_PASSWORD","value":"'"$(echo -n 'yourpassword' | base64 -w0)"'"}]'

kubectl patch secret hermes-secret -n hermes --type='json' \
  -p='[{"op":"add","path":"/data/HERMES_JWT_SECRET","value":"'"$(openssl rand -hex 32 | base64 -w0)"'"}]'
```

:::warning
`HERMES_JWT_SECRET` must be set before first boot. All existing sessions are invalidated if it changes.
:::

## Session Flow

```
Login:
  POST /auth/login  →  validates password, issues access JWT (15m) + refresh token (7d)
                    →  sets httpOnly cookies: access_token, refresh_token, csrf_token

Auto-refresh (client):
  POST /auth/refresh (every 12 min)  →  rotates refresh token, issues new access JWT

Logout:
  POST /auth/logout  →  revokes refresh token, clears all cookies
```

The access token is a signed JWT carrying `{ sub, email, role }`. It is never stored server-side — only the refresh token hash is in the database.

## CSRF Protection

All state-changing requests (POST, PATCH, DELETE) require an `X-CSRF-Token` header matching the `csrf_token` cookie. The cookie is readable by JavaScript (not httpOnly), so the dashboard reads it and sends it as a header.

Service-to-service calls using `Authorization: Bearer <HERMES_INTERNAL_TOKEN>` bypass CSRF — this is the machine-to-machine path used by K8s health probes and internal services.

## Users API

### Login

```http
POST /auth/login
Content-Type: application/json

{ "email": "admin@example.com", "password": "yourpassword" }
```

```json
200 → { "user": { "id": "usr_...", "email": "...", "role": "admin", ... } }
401 → { "error": "invalid_credentials" }
423 → { "error": "account_locked", "retry_after": 900 }
429 → { "error": "rate_limited" }
```

### Current user

```http
GET /auth/me
```

```json
{
  "user": { "id": "...", "email": "...", "role": "admin", "display_name": "Admin" },
  "settings": { "ui_theme": "midnight", "default_soul": null, ... },
  "permissions": ["delete_instance", "manage_platform", "manage_users", ...]
}
```

### Update profile / settings

```http
PATCH /users/me
X-CSRF-Token: <csrf_token>
Content-Type: application/json

{
  "display_name": "Admin",
  "ui_theme": "dusk",
  "default_soul": "general",
  "notification_telegram": true
}
```

### Create a user (admin only)

```http
POST /users
X-CSRF-Token: <csrf_token>
Content-Type: application/json

{
  "email": "alice@example.com",
  "username": "alice",
  "password": "temporary-password",
  "role": "user",
  "display_name": "Alice"
}
```

### Update a user (admin only)

```http
PATCH /users/:id
X-CSRF-Token: <csrf_token>
Content-Type: application/json

{ "role": "operator", "status": "active" }
```

Valid `status` values: `active`, `suspended`, `pending`.

### List users (admin only)

```http
GET /users?page=1&limit=20&role=user&status=active
```

## Audit Logs

Every significant action is written to `audit_logs` in `auth.db`:

| Action | Trigger |
|--------|---------|
| `login` | Successful sign-in |
| `login_failed` | Wrong password (increments lockout counter) |
| `logout` | Explicit sign-out |
| `spawn_instance` | New agent instance created |
| `delete_instance` | Agent instance deleted |
| `create_user` | Admin creates a new user |
| `update_user` | Admin changes role or status |

Query audit logs:

```http
GET /audit-logs?page=1&limit=50&action=spawn_instance
```

Requires `view_audit_logs` permission (admin or operator).

## Brute Force Protection

- **Per-email lockout**: 10 failed attempts in any window → account locked for 15 minutes
- **Per-IP rate limit**: 30 requests/minute to `/auth/login` → 429
- **Enumeration prevention**: "unknown email" and "wrong password" return the same `invalid_credentials` error with equal timing

## User Settings

Settings are per-user and stored in `auth.db`. They are returned with every `/auth/me` response and synced to the dashboard on load.

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `ui_theme` | string | `midnight` | Dashboard theme — synced server-side |
| `default_soul` | string | null | Pre-selected soul in the spawn modal |
| `default_model` | string | null | Pre-filled model in spawn form |
| `notification_telegram` | bool | false | Agent completion Telegram pings |
| `spawn_defaults` | JSON | null | Default max turns, context, etc. |

Theme is now synced to the server, not just localStorage — it survives clearing browser storage and transfers to other browsers.

## Dashboard UI

Once logged in, the dashboard shows:

- **Role badge** in the header — colour-coded (`admin` = red, `operator` = indigo, `user`/`viewer` = grey)
- **Display name or email** next to the badge
- **Sign out** button (→□ icon) in the header
- **Spawn panel** — hidden if you lack `spawn_instance` or `spawn_instance_restricted`
- **Delete button** — hidden if you lack `delete_instance`
- **Provider toggle** — protected; returns 403 if you lack `manage_platform`

## M2M / Service Bypass

Internal services (K8s health probes, `ai-router`, `inspector-mcp`) authenticate via the `HERMES_INTERNAL_TOKEN` Bearer header. This path bypasses cookie auth and CSRF and is treated as an `admin` service account.

The `/health` endpoint is always public — no auth required.

## Database

Auth data lives in `~/.logos/auth.db` alongside the existing `state.db`. Tables:

| Table | Contents |
|-------|----------|
| `users` | Accounts, password hashes, roles, lockout state |
| `refresh_tokens` | Token hashes, expiry, revocation |
| `user_settings` | Per-user preferences |
| `platform_settings` | Global platform config (single row) |
| `audit_logs` | Immutable event log |

The database uses WAL mode and foreign key constraints. It is never deleted on restart — schema migrations run via `CREATE TABLE IF NOT EXISTS`.

## Kubernetes Notes

The auth DB is stored on the same PVC as the rest of Hermes state (`hermes-pvc` → `~/.logos/`). It persists across pod restarts and upgrades — no migration step needed.

`HERMES_COOKIE_SECURE` defaults to `false` for HTTP LAN deployments. Set it to `true` when running behind an HTTPS reverse proxy:

```yaml
# In 01-configmap-env.yaml
HERMES_COOKIE_SECURE: "true"
```

## Related

- [Security](/docs/user-guide/security) — network isolation, dangerous command approval, container sandboxing
- [Soul Registry](/docs/guides/use-soul-with-hermes#soul-registry-k8s--multi-instance) — `user_accessible` flag that controls which souls non-admin users can spawn
- [Environment Variables](/docs/reference/environment-variables) — full list including auth vars
