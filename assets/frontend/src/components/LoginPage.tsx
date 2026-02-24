import type React from "react";
import { useState, useEffect, useRef, useCallback } from "react";
import { getAuthUrl } from "@/lib/api";
import styles from "@/styles/Login.module.css";

type LoginStep = "google" | "setup" | "verify";

declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (config: {
            client_id: string;
            callback: (response: { credential: string }) => void;
            auto_select?: boolean;
          }) => void;
          renderButton: (
            element: HTMLElement,
            config: {
              theme?: string;
              size?: string;
              width?: number;
              text?: string;
            },
          ) => void;
        };
      };
    };
  }
}

const GOOGLE_CLIENT_ID = import.meta.env.VITE_GOOGLE_CLIENT_ID || "";

interface LoginPageProps {
  onLoginSuccess: (token: string, email: string) => void;
}

export default function LoginPage({ onLoginSuccess }: LoginPageProps) {
  const [step, setStep] = useState<LoginStep>("google");
  const [googleToken, setGoogleToken] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [qrCode, setQrCode] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const googleButtonRef = useRef<HTMLDivElement>(null);

  const handleGoogleResponse = useCallback(
    async (response: { credential: string }) => {
      const token = response.credential;
      setGoogleToken(token);
      setError(null);
      setLoading(true);

      try {
        const res = await fetch(getAuthUrl("/auth/login"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ google_token: token }),
        });
        const data = await res.json();

        if (!res.ok) {
          setError(data.detail || "Email not authorized");
          return;
        }

        setEmail(data.email);

        if (data.requires_setup) {
          setQrCode(data.qr_code);
          setStep("setup");
        } else {
          setStep("verify");
        }
      } catch {
        setError("Unable to connect to server");
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (step !== "google" || !googleButtonRef.current) return;

    const initializeGoogle = () => {
      if (!window.google) return;
      window.google.accounts.id.initialize({
        client_id: GOOGLE_CLIENT_ID,
        callback: handleGoogleResponse,
      });
      window.google.accounts.id.renderButton(googleButtonRef.current!, {
        theme: "outline",
        size: "large",
        width: 340,
        text: "signin_with",
      });
    };

    // GIS script may already be loaded
    if (window.google) {
      initializeGoogle();
    } else {
      // Wait for the script to load
      const interval = setInterval(() => {
        if (window.google) {
          clearInterval(interval);
          initializeGoogle();
        }
      }, 100);
      return () => clearInterval(interval);
    }
  }, [step, handleGoogleResponse]);

  const handleCodeSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (code.length !== 6 || !googleToken) return;
    setError(null);
    setLoading(true);

    try {
      const res = await fetch(getAuthUrl("/auth/verify"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ google_token: googleToken, code }),
      });
      const data = await res.json();

      if (!res.ok) {
        setError(data.detail || "Invalid code");
        return;
      }

      onLoginSuccess(data.token, data.email);
    } catch {
      setError("Unable to connect to server");
    } finally {
      setLoading(false);
    }
  };

  const handleBack = () => {
    setStep("google");
    setGoogleToken(null);
    setEmail("");
    setCode("");
    setQrCode(null);
    setError(null);
  };

  return (
    <div className={styles.loginContainer}>
      <div className={styles.loginCard}>
        <h1 className={styles.title}>Spark Chat</h1>

        {step === "google" && (
          <>
            <p className={styles.subtitle}>Sign in with your Google account</p>
            <div className={styles.googleButton} ref={googleButtonRef} />
            {loading && (
              <p className={styles.subtitle}>Verifying your account...</p>
            )}
            {error && <div className={styles.error}>{error}</div>}
          </>
        )}

        {step === "setup" && (
          <>
            <p className={styles.subtitle}>Set up your authenticator app</p>
            {email && <div className={styles.emailDisplay}>{email}</div>}
            <div className={styles.qrSection}>
              {qrCode && (
                <img
                  className={styles.qrImage}
                  src={`data:image/png;base64,${qrCode}`}
                  alt="QR code for authenticator app"
                />
              )}
              <p className={styles.instructions}>
                1. Open <strong>Microsoft Authenticator</strong> or{" "}
                <strong>Google Authenticator</strong>
                <br />
                2. Tap <strong>+</strong> then <strong>Other account</strong>
                <br />
                3. Scan the QR code above
                <br />
                4. Enter the 6-digit code shown in the app
              </p>
            </div>
            <form onSubmit={handleCodeSubmit} className={styles.form}>
              <div className={styles.inputGroup}>
                <label className={styles.label}>6-digit code</label>
                <input
                  type="text"
                  inputMode="numeric"
                  pattern="[0-9]*"
                  maxLength={6}
                  className={`${styles.input} ${styles.codeInput}`}
                  value={code}
                  onChange={(e) =>
                    setCode(e.target.value.replace(/\D/g, "").slice(0, 6))
                  }
                  placeholder="000000"
                  autoFocus
                  autoComplete="one-time-code"
                />
              </div>
              {error && <div className={styles.error}>{error}</div>}
              <button
                type="submit"
                className={styles.submitButton}
                disabled={loading || code.length !== 6}
              >
                {loading ? "Verifying..." : "Verify & Sign In"}
              </button>
            </form>
            <button className={styles.backButton} onClick={handleBack}>
              Back
            </button>
          </>
        )}

        {step === "verify" && (
          <>
            <p className={styles.subtitle}>
              Enter the code from your authenticator app
            </p>
            {email && <div className={styles.emailDisplay}>{email}</div>}
            <form onSubmit={handleCodeSubmit} className={styles.form}>
              <div className={styles.inputGroup}>
                <label className={styles.label}>6-digit code</label>
                <input
                  type="text"
                  inputMode="numeric"
                  pattern="[0-9]*"
                  maxLength={6}
                  className={`${styles.input} ${styles.codeInput}`}
                  value={code}
                  onChange={(e) =>
                    setCode(e.target.value.replace(/\D/g, "").slice(0, 6))
                  }
                  placeholder="000000"
                  autoFocus
                  autoComplete="one-time-code"
                />
              </div>
              {error && <div className={styles.error}>{error}</div>}
              <button
                type="submit"
                className={styles.submitButton}
                disabled={loading || code.length !== 6}
              >
                {loading ? "Verifying..." : "Sign In"}
              </button>
            </form>
            <button className={styles.backButton} onClick={handleBack}>
              Back
            </button>
          </>
        )}
      </div>
    </div>
  );
}
