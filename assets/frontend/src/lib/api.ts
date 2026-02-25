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

/**
 * Create auth headers for authenticated requests
 */
export function getAuthHeaders(token: string | null): Record<string, string> {
  if (!token) return {};
  return { Authorization: `Bearer ${token}` };
}

/**
 * Fetch wrapper that includes auth headers and handles 401 responses.
 * @param url - Request URL
 * @param token - JWT token
 * @param options - Fetch options
 * @param onUnauthorized - Callback when 401 is received (e.g., trigger logout)
 * @returns Fetch Response
 */
export async function authenticatedFetch(
  url: string,
  token: string | null,
  options: RequestInit = {},
  onUnauthorized?: () => void,
): Promise<Response> {
  const headers = new Headers(options.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const res = await fetch(url, { ...options, headers });

  if (res.status === 401 && onUnauthorized) {
    onUnauthorized();
  }

  return res;
}
