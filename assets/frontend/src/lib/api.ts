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

  const res = await fetch(getApiUrl(path), { ...options, headers });

  if (res.status === 401 && _onUnauthorized) {
    _onUnauthorized();
  }

  return res;
}

/**
 * Get the backend API base URL
 * Same-origin path prefix on bytecourier hosts; direct connection for local dev
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

    // Same-origin path prefix on bytecourier hosts
    if (hostname.includes('bytecourier')) {
      return `${protocol}//${hostname}/api/backend-svc`;
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

export interface ChatStreamEvent {
  type: string;
  // The backend uses both `data` (tokens, usage, node_start) and `messages`
  // (history) and `content` (errors) — keep this loose.
  [key: string]: unknown;
}

/**
 * Stream a chat query as Server-Sent Events.
 *
 * Uses fetch + ReadableStream rather than EventSource because EventSource
 * cannot set Authorization headers and cannot send a POST body. The handler
 * is called once per parsed SSE frame; callers should pass an AbortSignal
 * to support cancellation.
 */
export async function streamChatQuery(
  chatId: string,
  message: string,
  onEvent: (event: ChatStreamEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const res = await apiFetch(`/chat/${encodeURIComponent(chatId)}/query`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Accept': 'text/event-stream',
    },
    body: JSON.stringify({ message }),
    signal,
  });

  if (!res.ok || !res.body) {
    throw new Error(`Stream failed: ${res.status} ${res.statusText}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by blank lines. We only care about `data:`
    // lines — the backend doesn't emit `event:` or `id:`.
    let sep: number;
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const dataLine = frame
        .split('\n')
        .filter((l) => l.startsWith('data:'))
        .map((l) => l.slice(5).trimStart())
        .join('\n');
      if (!dataLine) continue;
      try {
        onEvent(JSON.parse(dataLine) as ChatStreamEvent);
      } catch (e) {
        console.warn('Failed to parse SSE frame', e, dataLine);
      }
    }
  }
}

/**
 * Get the auth service base URL.
 * Auth service is at auth.bytecourier.* with /api/svc path prefix.
 */
export function getAuthUrl(path: string): string {
  const normalized = path.startsWith('/') ? path : `/${path}`;
  const authUrl = import.meta.env.VITE_AUTH_URL;
  if (authUrl) {
    return `${authUrl}${normalized}`;
  }
  if (typeof window !== 'undefined') {
    const { protocol, hostname } = window.location;
    if (hostname.includes('bytecourier')) {
      const authHostname = hostname.replace(/^sparkchat\./, 'auth.');
      return `${protocol}//${authHostname}/api/svc${normalized}`;
    }
    // Local dev: use auth.bytecourier.local
    return `${protocol}//auth.bytecourier.local/api/svc${normalized}`;
  }
  return `http://auth.bytecourier.local/api/svc${normalized}`;
}

