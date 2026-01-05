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

    // Replace 'frontend' with 'backend' in hostname
    if (hostname.startsWith('frontend.')) {
      const backendHostname = hostname.replace('frontend.', 'backend.');
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
export function getWebSocketUrl(path: string): string {
  // Ensure path starts with /
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;

  // Use configured backend WebSocket URL if available
  const backendWsUrl = import.meta.env.VITE_BACKEND_WS_URL;
  if (backendWsUrl) {
    return `${backendWsUrl}${normalizedPath}`;
  }

  // Derive from backend URL
  const backendUrl = getBackendUrl();
  const wsUrl = backendUrl
    .replace('https://', 'wss://')
    .replace('http://', 'ws://');

  return `${wsUrl}${normalizedPath}`;
}
