import type React from "react";
import { useState } from "react";
import { getAuthUrl } from "@/lib/api";
import styles from "@/styles/Login.module.css";

type LoginStep = "email" | "setup" | "verify";

interface LoginPageProps {
  onLoginSuccess: (token: string, email: string) => void;
}

export default function LoginPage({ onLoginSuccess }: LoginPageProps) {
  const [step, setStep] = useState<LoginStep>("email");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [qrCode, setQrCode] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleEmailSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!email.trim()) return;
    setError(null);
    setLoading(true);

    try {
      const res = await fetch(getAuthUrl("/auth/login"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email.trim() }),
      });
      const data = await res.json();

      if (!res.ok) {
        setError(data.detail || "Email not authorized");
        return;
      }

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
  };

  const handleCodeSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (code.length !== 6) return;
    setError(null);
    setLoading(true);

    try {
      const res = await fetch(getAuthUrl("/auth/verify"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email.trim(), code }),
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
    setStep("email");
    setCode("");
    setQrCode(null);
    setError(null);
  };

  return (
    <div className={styles.loginContainer}>
      <div className={styles.loginCard}>
        <h1 className={styles.title}>Spark Chat</h1>

        {step === "email" && (
          <>
            <p className={styles.subtitle}>Enter your email to sign in</p>
            <form onSubmit={handleEmailSubmit} className={styles.form}>
              <div className={styles.inputGroup}>
                <label className={styles.label}>Email address</label>
                <input
                  type="email"
                  className={styles.input}
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                  autoFocus
                  required
                />
              </div>
              {error && <div className={styles.error}>{error}</div>}
              <button
                type="submit"
                className={styles.submitButton}
                disabled={loading || !email.trim()}
              >
                {loading ? "Checking..." : "Continue"}
              </button>
            </form>
          </>
        )}

        {step === "setup" && (
          <>
            <p className={styles.subtitle}>
              Set up your authenticator app
            </p>
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
