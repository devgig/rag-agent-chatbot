const TOKEN_KEY = "spark_chat_token";
const EMAIL_KEY = "spark_chat_email";

// In-memory primary storage — not accessible via devtools Storage tab
let _memToken: string | null = null;
let _memEmail: string | null = null;

export function getToken(): string | null {
  return _memToken ?? sessionStorage.getItem(TOKEN_KEY);
}

export function getEmail(): string | null {
  return _memEmail ?? sessionStorage.getItem(EMAIL_KEY);
}

export function setAuth(token: string, email: string): void {
  _memToken = token;
  _memEmail = email;
  // sessionStorage survives page refresh but clears on tab close
  sessionStorage.setItem(TOKEN_KEY, token);
  sessionStorage.setItem(EMAIL_KEY, email);
}

export function clearAuth(): void {
  _memToken = null;
  _memEmail = null;
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(EMAIL_KEY);
  // Clean up any legacy localStorage entries
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(EMAIL_KEY);
}

export function isTokenExpired(token: string): boolean {
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    return payload.exp * 1000 < Date.now();
  } catch {
    return true;
  }
}

/** Returns the token's expiry time in ms since epoch, or 0 on error. */
export function getTokenExpiry(token: string): number {
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    return payload.exp * 1000;
  } catch {
    return 0;
  }
}
