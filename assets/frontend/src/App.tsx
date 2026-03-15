/*
# SPDX-FileCopyrightText: Copyright (c) 1993-2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
*/
import { useState, useRef, useEffect, useCallback } from 'react';
import QuerySection from '@/components/QuerySection';
import DocumentIngestion from '@/components/DocumentIngestion';
import Sidebar from '@/components/Sidebar';
import ThemeToggle from '@/components/ThemeToggle';
import styles from '@/styles/Home.module.css';
import { getAuthUrl, apiFetch, setOnUnauthorized } from '@/lib/api';
import { getToken, getEmail, setAuth, clearAuth, isTokenExpired, getTokenExpiry } from '@/lib/auth';

const LOGGED_OUT_KEY = "spark_chat_logged_out";

function getAuthHost(): string {
  const { hostname } = window.location;
  if (hostname.includes('bytecourier')) {
    return hostname.replace(/^sparkchat\./, 'auth.');
  }
  return 'auth.bytecourier.local';
}

function redirectToAuthLogin() {
  const { protocol } = window.location;
  const redirectUri = encodeURIComponent(window.location.origin);
  window.location.href = `${protocol}//${getAuthHost()}?redirect_uri=${redirectUri}`;
}

function redirectToAuthLogout() {
  const { protocol } = window.location;
  const redirectUri = encodeURIComponent(window.location.origin);
  window.location.href = `${protocol}//${getAuthHost()}/logout?redirect_uri=${redirectUri}`;
}

export default function App() {
  const [query, setQuery] = useState("");
  const [response, setResponse] = useState("[]");
  const [files, setFiles] = useState<FileList | null>(null);
  const [ingestMessage, setIngestMessage] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [isIngesting, setIsIngesting] = useState(false);
  const [showIngestion, setShowIngestion] = useState(false);
  const [refreshTrigger, setRefreshTrigger] = useState(0);
  const [currentChatId, setCurrentChatId] = useState<string | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  // Auth state
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [authChecked, setAuthChecked] = useState(false);

  // Authenticate: accept fresh token from URL hash, or check stored token locally
  useEffect(() => {
    // Fresh token in URL hash (just returned from auth service)
    const hash = window.location.hash;
    if (hash.includes('token=')) {
      const params = new URLSearchParams(hash.substring(1));
      const token = params.get('token');
      const email = params.get('email');
      if (token && email && !isTokenExpired(token)) {
        setAuth(token, decodeURIComponent(email));
        // Successful login clears the logged-out flag
        sessionStorage.removeItem(LOGGED_OUT_KEY);
        history.replaceState(null, '', window.location.pathname + window.location.search);
        setIsAuthenticated(true);
        setAuthChecked(true);
        return;
      }
    }

    // Stored token — validate locally (backend validates via JWKS on API calls)
    const token = getToken();
    if (token && !isTokenExpired(token)) {
      setIsAuthenticated(true);
    } else {
      clearAuth();
    }
    setAuthChecked(true);
  }, []);

  // Session expired — silently redirect to auth login for a new token.
  // Does NOT clear the SSO session so the user is re-authenticated seamlessly.
  const handleSessionExpired = useCallback(() => {
    clearAuth();
    setIsAuthenticated(false);
    setCurrentChatId(null);
    setResponse("[]");
    redirectToAuthLogin();
  }, []);

  // Explicit sign-out — clears local auth AND the SSO session cookie.
  // Sets a flag so the app shows a login page instead of auto-redirecting.
  const handleLogout = useCallback(() => {
    clearAuth();
    setIsAuthenticated(false);
    setCurrentChatId(null);
    setResponse("[]");
    sessionStorage.setItem(LOGGED_OUT_KEY, "1");
    redirectToAuthLogout();
  }, []);

  // Register global 401 handler — uses session-expired (silent re-auth),
  // NOT full logout, to avoid clearing the SSO session on token expiry.
  useEffect(() => {
    setOnUnauthorized(handleSessionExpired);
  }, [handleSessionExpired]);

  // Proactive token refresh — refreshes 2 minutes before expiry
  useEffect(() => {
    if (!isAuthenticated) return;

    const scheduleRefresh = () => {
      const token = getToken();
      if (!token) return undefined;

      const expiresAt = getTokenExpiry(token);
      const delay = expiresAt - 2 * 60 * 1000 - Date.now(); // 2 min before exp

      if (delay <= 0) {
        // Already within the refresh window — refresh now
        refreshToken();
        return undefined;
      }

      return setTimeout(refreshToken, delay);
    };

    const refreshToken = async () => {
      const token = getToken();
      if (!token || isTokenExpired(token)) {
        handleSessionExpired();
        return;
      }
      try {
        const res = await fetch(getAuthUrl("/auth/refresh"), {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
        });
        if (res.ok) {
          const data = await res.json();
          const email = getEmail();
          if (email) setAuth(data.token, email);
          // Schedule next refresh with the new token
          timerId = scheduleRefresh();
        } else {
          handleSessionExpired();
        }
      } catch {
        handleSessionExpired();
      }
    };

    let timerId = scheduleRefresh();
    return () => { if (timerId) clearTimeout(timerId); };
  }, [isAuthenticated, handleSessionExpired]);

  // Always start a fresh chat on page load
  useEffect(() => {
    if (!isAuthenticated) return;
    const createFreshChat = async () => {
      try {
        const res = await apiFetch("/chat/new", { method: "POST" });
        if (res.ok) {
          const { chat_id } = await res.json();
          setCurrentChatId(chat_id);
        }
      } catch (error) {
        console.error("Error creating new chat:", error);
      }
    };
    createFreshChat();
  }, [isAuthenticated]);

  // Handle chat changes
  const handleChatChange = async (newChatId: string) => {
    try {
      const res = await apiFetch("/chat_id", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: newChatId })
      });

      if (res.ok) {
        setCurrentChatId(newChatId);
        setResponse("[]");
      }
    } catch (error) {
      console.error("Error updating chat ID:", error);
    }
  };

  // Clean up any ongoing streams when component unmounts
  useEffect(() => {
    return () => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
    };
  }, []);

  // Function to handle successful document ingestion
  const handleSuccessfulIngestion = () => {
    setRefreshTrigger(prev => prev + 1);
  };

  if (!authChecked) {
    return null; // Avoid flash while checking token
  }

  // Not authenticated — either show login page or silently redirect
  if (!isAuthenticated) {
    // After explicit sign-out: show a login button instead of auto-redirecting
    // (auto-redirect would just re-authenticate via upstream IdP session)
    if (sessionStorage.getItem(LOGGED_OUT_KEY)) {
      return (
        <div style={{
          display: 'flex', flexDirection: 'column', alignItems: 'center',
          justifyContent: 'center', height: '100vh', gap: '1rem',
          fontFamily: 'system-ui, sans-serif',
        }}>
          <p style={{ fontSize: '1.1rem', color: 'var(--text-secondary, #666)' }}>
            You have been signed out.
          </p>
          <button
            onClick={() => {
              sessionStorage.removeItem(LOGGED_OUT_KEY);
              redirectToAuthLogin();
            }}
            style={{
              padding: '0.6rem 1.5rem', fontSize: '1rem', cursor: 'pointer',
              borderRadius: '8px', border: '1px solid #ccc',
              background: 'var(--bg-primary, #fff)', color: 'var(--text-primary, #333)',
            }}
          >
            Sign In
          </button>
        </div>
      );
    }
    // Token expired or first visit — silently redirect to auth service
    redirectToAuthLogin();
    return null;
  }

  return (
    <>
      <ThemeToggle />
      <div className={styles.container}>
        <Sidebar
          showIngestion={showIngestion}
          setShowIngestion={setShowIngestion}
          refreshTrigger={refreshTrigger}
          currentChatId={currentChatId}
          onChatChange={handleChatChange}
          onLogout={handleLogout}
        />

        <div className={styles.mainContent}>
          <QuerySection
            query={query}
            response={response}
            isStreaming={isStreaming}
            setQuery={setQuery}
            setResponse={setResponse}
            setIsStreaming={setIsStreaming}
            abortControllerRef={abortControllerRef}
            setShowIngestion={setShowIngestion}
            currentChatId={currentChatId}
          />
        </div>

        {showIngestion && (
          <>
            <div className={styles.overlay} onClick={() => setShowIngestion(false)} />
            <div className={styles.documentUploadContainer}>
              <button
                className={styles.closeButton}
                onClick={() => setShowIngestion(false)}
              >
                ×
              </button>
              <DocumentIngestion
                files={files}
                ingestMessage={ingestMessage}
                isIngesting={isIngesting}
                setFiles={setFiles}
                setIngestMessage={setIngestMessage}
                setIsIngesting={setIsIngesting}
                onSuccessfulIngestion={handleSuccessfulIngestion}
              />
            </div>
          </>
        )}
      </div>
    </>
  );
}
