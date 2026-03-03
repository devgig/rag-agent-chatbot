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

import { getToken, isTokenExpired } from './auth';

let _onUnauthorized: (() => void) | null = null;

export function setOnUnauthorized(cb: () => void): void {
  _onUnauthorized = cb;
}

export function triggerUnauthorized(): void {
  if (_onUnauthorized) _onUnauthorized();
}

/**
 * Fetch wrapper for backend API calls.
 * Reads JWT from localStorage at call time (never stale).
 * Attaches Authorization header and handles 401 automatically.
 */
export async function apiFetch(
  path: string,
  options: RequestInit = {},
): Promise<Response> {
  const token = getToken();

  // Reject expired tokens locally — avoids sending them to Istio which
  // returns 403 without CORS headers, masking the real auth failure.
  if (!token || isTokenExpired(token)) {
    if (_onUnauthorized) _onUnauthorized();
    return new Response(null, { status: 401, statusText: "Token expired" });
  }

  const headers = new Headers(options.headers);
  headers.set("Authorization", `Bearer ${token}`);

  const url = getApiUrl(path);
  const fetchOpts = { ...options, headers };

  // Retry transient failures (503, network errors) up to 3 times
  let res: Response;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      res = await fetch(url, fetchOpts);
      if (res.status !== 503) break;
    } catch (err) {
      if (attempt === 2) throw err;
    }
    await new Promise((r) => setTimeout(r, 500 * (attempt + 1)));
  }

  if (res!.status === 401 && _onUnauthorized) {
    _onUnauthorized();
  }

  return res!;
}

/**
 * Get the backend API base URL
 * Connects directly to backend (no proxy needed - CORS is configured)
 */
export function getBackendUrl(): string {
  // Use configured backend URL if available (set at build time)
  const backendUrl = import.meta.env.VITE_BACKEND_URL;
  if (backendUrl) {
    return backendUrl;
  }

  // Derive backend URL from frontend hostname
  if (typeof window !== 'undefined') {
    const { protocol, hostname } = window.location;

    // Replace 'sparkchat' with 'sparkbackend' in hostname
    if (hostname.startsWith('sparkchat.')) {
      const backendHostname = hostname.replace('sparkchat.', 'sparkbackend.');
      return `${protocol}//${backendHostname}`;
    }

    // For local development, use same host with port 8000
    return `${protocol}//${hostname}:8000`;
  }

  // Fallback for SSR/build time
  return 'http://localhost:8000';
}

/**
 * Construct full API URL from a path
 * @param path - API path (e.g., '/sources', '/chats')
 * @returns Full URL to backend API
 */
export function getApiUrl(path: string): string {
  const backendUrl = getBackendUrl();
  // Ensure path starts with /
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  return `${backendUrl}${normalizedPath}`;
}

/**
 * Get WebSocket URL for real-time communication
 * @param path - WebSocket path (e.g., '/ws/chat/123')
 * @returns WebSocket URL
 */
export function getWebSocketUrl(path: string, token?: string | null): string {
  // Ensure path starts with /
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;

  // Use configured backend WebSocket URL if available
  const backendWsUrl = import.meta.env.VITE_BACKEND_WS_URL;
  let base: string;
  if (backendWsUrl) {
    base = `${backendWsUrl}${normalizedPath}`;
  } else {
    const backendUrl = getBackendUrl();
    const wsUrl = backendUrl
      .replace('https://', 'wss://')
      .replace('http://', 'ws://');
    base = `${wsUrl}${normalizedPath}`;
  }

  // Append token as query param if provided
  if (token) {
    const separator = base.includes('?') ? '&' : '?';
    return `${base}${separator}token=${encodeURIComponent(token)}`;
  }
  return base;
}

/**
 * Get the auth service base URL
 */
export function getAuthUrl(path: string): string {
  const authUrl = import.meta.env.VITE_AUTH_URL;
  if (authUrl) {
    return `${authUrl}${path.startsWith('/') ? path : `/${path}`}`;
  }
  // Derive from hostname: replace 'sparkchat' with 'auth'
  if (typeof window !== 'undefined') {
    const { protocol, hostname } = window.location;
    if (hostname.includes('bytecourier')) {
      const authHostname = hostname.replace(/^sparkchat\./, 'auth.');
      return `${protocol}//${authHostname}${path.startsWith('/') ? path : `/${path}`}`;
    }
    // Local dev: same host, port 8001
    return `${protocol}//${hostname}:8001${path.startsWith('/') ? path : `/${path}`}`;
  }
  return `http://localhost:8001${path.startsWith('/') ? path : `/${path}`}`;
}

