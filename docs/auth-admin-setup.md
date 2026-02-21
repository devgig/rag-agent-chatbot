# Authentication Admin Setup Guide

This guide covers how to configure and manage TOTP-based authentication for Spark Chat.

## Architecture Overview

- Users authenticate with **email + TOTP code** (from Microsoft Authenticator or Google Authenticator)
- An **allowlist** of authorized emails is stored in Azure Key Vault
- A **JWT secret** for signing tokens is stored in Azure Key Vault
- Both are pulled into Kubernetes via **ExternalSecret**
- JWT tokens expire after **30 minutes**

## Azure Key Vault Secrets

Create two secrets in your Azure Key Vault:

### `rag-agent-auth-users`

A colon-delimited list of authorized email addresses:

```
alice@company.com:bob@company.com:charlie@company.com
```

### `rag-agent-jwt-secret`

A strong random string used to sign JWT tokens. Generate one with:

```bash
openssl rand -base64 32
```

## Kubernetes Resources

The `auth-external-secret.yaml` manifest pulls both secrets into a Kubernetes secret named `auth-credentials`:

| Key | Source (Azure KV) | Purpose |
|---|---|---|
| `allowed-emails` | `rag-agent-auth-users` | Colon-delimited email allowlist |
| `jwt-secret` | `rag-agent-jwt-secret` | JWT signing key |

The backend deployment reads these as environment variables:

| Environment Variable | Secret Key | Purpose |
|---|---|---|
| `AUTH_ALLOWED_EMAILS` | `auth-credentials/allowed-emails` | Authorized emails |
| `JWT_SECRET` | `auth-credentials/jwt-secret` | JWT signing key |

### Optional Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `JWT_EXPIRATION_MINUTES` | `30` | Token lifetime in minutes |
| `TOTP_ISSUER` | `Spark Chat` | Name shown in authenticator apps |

## Managing Users

### Adding a User

1. Update the `rag-agent-auth-users` secret in Azure Key Vault — append `:newuser@company.com`
2. Restart the backend pod (or wait for ExternalSecret refresh)
3. The user can now sign in — they will see a QR code on first login to set up their authenticator app

### Removing a User

1. Update the `rag-agent-auth-users` secret in Azure Key Vault — remove the email
2. Restart the backend pod
3. The user's TOTP enrollment is automatically cleared and their account is disabled

### Resetting a User's TOTP

If a user loses access to their authenticator app:

1. Remove their email from the `rag-agent-auth-users` Key Vault secret
2. Restart the backend pod (clears their TOTP setup)
3. Re-add their email to the Key Vault secret
4. Restart the backend pod again
5. On next login, they will see a new QR code and can enroll a new device

## Local Development

Set the following environment variables for the backend:

```bash
export AUTH_ALLOWED_EMAILS="your@email.com"
export JWT_SECRET="dev-secret-change-in-production"
```

Then start the backend normally. The allowlist sync runs on startup.

## Auth Flow Summary

```
User → Email → POST /auth/login
                  ↓
         ┌── First time? ──┐
         │                  │
     QR Code            Code prompt
     + Code prompt          │
         │                  │
         ↓                  ↓
      POST /auth/verify (email + 6-digit code)
                  ↓
            JWT token (30 min)
                  ↓
       All API calls include:
       Authorization: Bearer <jwt>
       WebSocket: ?token=<jwt>
```
