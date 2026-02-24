# Google Sign-In Setup Guide

Google Sign-In is the first authentication gate for Spark Chat. Users must prove email ownership via Google before setting up or entering a TOTP code.

## Auth Flow

```
User → "Sign in with Google" → Google popup → ID token
                                                  ↓
                                        POST /auth/login (google_token)
                                                  ↓
                                      Verify token, check allowlist
                                                  ↓
                                    ┌── First time? ──┐
                                    │                  │
                                QR Code            Code prompt
                                + Code prompt          │
                                    │                  │
                                    ↓                  ↓
                              POST /auth/verify (google_token + 6-digit code)
                                                  ↓
                                            JWT token (30 min)
```

## Google Cloud Console Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com) > **APIs & Services** > **Credentials**
2. Click **Create Credentials** > **OAuth 2.0 Client ID**
3. Select **Web application** as the application type
4. Under **Authorized JavaScript origins**, add:
   - `http://localhost:3000` (local dev)
   - `http://sparkchat.bytecourier.local` (local cluster)
   - `https://sparkchat.bytecourier.com` (production)
5. Click **Create** and copy the **Client ID** (format: `xxxx.apps.googleusercontent.com`)
6. No client secret is needed — this uses the GIS credential/popup flow

## Azure Key Vault

Store the Client ID in Azure Key Vault:

```bash
az keyvault secret set \
  --vault-name <your-vault-name> \
  --name google-client-id \
  --value "<your-google-client-id>"
```

This is pulled into Kubernetes via the `auth-external-secret.yaml` as part of the `auth-credentials` secret.

## Kubernetes Secret Mapping

| Secret Key | Azure KV Key | Purpose |
|---|---|---|
| `google-client-id` | `google-client-id` | Google OAuth Client ID |

The deployment reads it as:

| Environment Variable | Secret Key | Purpose |
|---|---|---|
| `GOOGLE_CLIENT_ID` | `auth-credentials/google-client-id` | Passed to `google.oauth2.id_token.verify_oauth2_token()` |

## Frontend Configuration

The frontend needs the Google Client ID at build time:

```bash
export VITE_GOOGLE_CLIENT_ID="<your-google-client-id>"
```

Or add to `.env`:

```
VITE_GOOGLE_CLIENT_ID=xxxx.apps.googleusercontent.com
```

The GIS library is loaded via a script tag in `index.html`:

```html
<script src="https://accounts.google.com/gsi/client" async defer></script>
```

## Local Development

Set the following environment variables:

```bash
# Backend (signra)
export GOOGLE_CLIENT_ID="<your-google-client-id>"
export AUTH_ALLOWED_EMAILS="your@gmail.com"
export JWT_PRIVATE_KEY="$(cat path/to/private-key.pem)"

# Frontend
export VITE_GOOGLE_CLIENT_ID="<your-google-client-id>"
```

For local dev, make sure `http://localhost:3000` is in your Google OAuth authorized origins.

## Verification Checklist

1. **Google Sign-In button renders** — GIS script loads, button appears on login page
2. **Google popup works** — clicking the button opens Google's sign-in popup
3. **Allowed email** — backend verifies Google token, checks allowlist, returns QR or code prompt
4. **Disallowed email** — backend returns 403
5. **Invalid/expired Google token** — backend returns 401
6. **TOTP setup** — QR code displayed after first Google sign-in for new users
7. **Returning user** — Google Sign-In then TOTP code prompt, no QR code

## Troubleshooting

| Issue | Cause | Fix |
|---|---|---|
| "Google auth not configured" (500) | `GOOGLE_CLIENT_ID` env var not set | Check ExternalSecret sync, pod env |
| "Invalid Google token" (401) | Token expired or wrong client ID | Ensure frontend and backend use the same Client ID |
| "Email not verified by Google" (401) | Google account email not verified | User must verify their email with Google |
| Google button doesn't appear | GIS script failed to load | Check browser console, CSP headers |
| Popup blocked | Browser blocked the Google popup | User must allow popups for the site |
