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
 * Uses external DNS name for direct backend access
 */
export function getBackendUrl(): string {
  // Use environment variable if set, otherwise use external DNS
  return process.env.NEXT_PUBLIC_API_URL || 'http://backend.bytecourier.local:8000';
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
