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
import { useState, useRef, useEffect } from 'react';
import QuerySection from '@/components/QuerySection';
import DocumentIngestion from '@/components/DocumentIngestion';
import LoginPage from '@/components/LoginPage';
import Sidebar from '@/components/Sidebar';
import ThemeToggle from '@/components/ThemeToggle';
import styles from '@/styles/Home.module.css';
import { getApiUrl, getAuthUrl } from '@/lib/api';
import { getToken, setAuth, clearAuth, isTokenExpired } from '@/lib/auth';

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
  const [authToken, setAuthToken] = useState<string | null>(null);
  const [authChecked, setAuthChecked] = useState(false);

  // Check for existing token on mount
  useEffect(() => {
    const checkAuth = async () => {
      const token = getToken();
      if (!token || isTokenExpired(token)) {
        clearAuth();
        setAuthChecked(true);
        return;
      }
      try {
        const res = await fetch(getAuthUrl("/auth/me"), {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (res.ok) {
          setAuthToken(token);
          setIsAuthenticated(true);
        } else {
          clearAuth();
        }
      } catch {
        clearAuth();
      }
      setAuthChecked(true);
    };
    checkAuth();
  }, []);

  const handleLoginSuccess = (token: string, email: string) => {
    setAuth(token, email);
    setAuthToken(token);
    setIsAuthenticated(true);
  };

  const handleLogout = () => {
    clearAuth();
    setAuthToken(null);
    setIsAuthenticated(false);
    setCurrentChatId(null);
    setResponse("[]");
  };

  // Load initial chat ID (only when authenticated)
  useEffect(() => {
    if (!isAuthenticated || !authToken) return;
    const fetchCurrentChatId = async () => {
      try {
        const response = await fetch(getApiUrl("/chat_id"), {
          headers: { Authorization: `Bearer ${authToken}` },
        });
        if (response.ok) {
          const { chat_id } = await response.json();
          setCurrentChatId(chat_id);
        } else if (response.status === 401) {
          handleLogout();
        }
      } catch (error) {
        console.error("Error fetching current chat ID:", error);
      }
    };
    fetchCurrentChatId();
  }, [isAuthenticated, authToken]);

  // Handle chat changes
  const handleChatChange = async (newChatId: string) => {
    try {
      const response = await fetch(getApiUrl("/chat_id"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
        },
        body: JSON.stringify({ chat_id: newChatId })
      });

      if (response.ok) {
        setCurrentChatId(newChatId);
        setResponse("[]");
      } else if (response.status === 401) {
        handleLogout();
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

  if (!isAuthenticated) {
    return (
      <>
        <ThemeToggle />
        <LoginPage onLoginSuccess={handleLoginSuccess} />
      </>
    );
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
          token={authToken}
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
            token={authToken}
            onLogout={handleLogout}
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
                token={authToken}
              />
            </div>
          </>
        )}
      </div>
    </>
  );
}
