# Authenticator App Setup Guide

Spark Chat uses TOTP (Time-based One-Time Passwords) for authentication. You'll need an authenticator app on your phone to generate login codes.

## 1. Install an Authenticator App

Install one of the following on your phone:

- **Microsoft Authenticator** — [iOS](https://apps.apple.com/app/microsoft-authenticator/id983156458) | [Android](https://play.google.com/store/apps/details?id=com.azure.authenticator)
- **Google Authenticator** — [iOS](https://apps.apple.com/app/google-authenticator/id388497605) | [Android](https://play.google.com/store/apps/details?id=com.google.android.apps.authenticator2)

## 2. First-Time Sign In

1. Open Spark Chat in your browser
2. Click **Sign in with Google** and select your authorized Google account
3. A QR code will appear on screen

### Scan the QR Code

**Microsoft Authenticator:**
1. Open the app
2. Tap **+** (top right)
3. Select **Other account**
4. Point your camera at the QR code on screen
5. The app will add an entry labeled **Spark Chat (your@email.com)**

**Google Authenticator:**
1. Open the app
2. Tap **+** (bottom right)
3. Select **Scan a QR code**
4. Point your camera at the QR code on screen
5. The app will add an entry labeled **Spark Chat (your@email.com)**

### Enter the Code

1. Your authenticator app now shows a 6-digit code that refreshes every 30 seconds
2. Type this code into the login page
3. Click **Verify & Sign In**
4. You're now logged in

## 3. Daily Login

1. Open Spark Chat
2. Click **Sign in with Google** and select your account
3. Open your authenticator app and find the **Spark Chat** entry
4. Type the current 6-digit code shown in the app
5. Click **Sign In**

The code changes every 30 seconds. If a code doesn't work, wait for the next one.

## 4. Session Duration

Your login session lasts **30 minutes**. After that, you'll be redirected to the login page and can sign in again with a new code from your authenticator app.

## 5. Lost Access / New Phone

If you lose access to your authenticator app (lost phone, new device, app reinstalled):

1. Contact your administrator
2. They will reset your TOTP enrollment
3. On your next login, you'll see the QR code again to set up your new device
