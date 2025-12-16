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

import { NextRequest, NextResponse } from 'next/server';

// Backend service URL - uses internal Kubernetes DNS
const BACKEND_URL = process.env.BACKEND_URL || 'http://multi-agent-backend.multi-agent-dev.svc.cluster.local:8000';

// Helper function to get backend URL for a given path
function getBackendUrl(path: string[]): string {
  const pathStr = path.join('/');
  return `${BACKEND_URL}/${pathStr}`;
}

// Handle GET requests
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const { path } = await params;
  const backendUrl = getBackendUrl(path);

  // Forward query parameters
  const url = new URL(backendUrl);
  request.nextUrl.searchParams.forEach((value, key) => {
    url.searchParams.append(key, value);
  });

  try {
    const response = await fetch(url.toString(), {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json',
      },
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error('API proxy error:', error);
    return NextResponse.json(
      { error: 'Failed to fetch from backend' },
      { status: 500 }
    );
  }
}

// Handle POST requests
export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const { path } = await params;
  const backendUrl = getBackendUrl(path);

  try {
    // Check if this is a multipart/form-data request (file upload)
    const contentType = request.headers.get('content-type') || '';
    let body;
    let headers: HeadersInit = {};

    if (contentType.includes('multipart/form-data')) {
      // For file uploads, forward the FormData directly
      body = await request.formData();
      // Don't set Content-Type header - fetch will set it with boundary
    } else {
      // For JSON requests
      body = JSON.stringify(await request.json());
      headers['Content-Type'] = 'application/json';
    }

    const response = await fetch(backendUrl, {
      method: 'POST',
      headers,
      body,
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error('API proxy error:', error);
    return NextResponse.json(
      { error: 'Failed to post to backend' },
      { status: 500 }
    );
  }
}

// Handle DELETE requests
export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const { path } = await params;
  const backendUrl = getBackendUrl(path);

  try {
    const response = await fetch(backendUrl, {
      method: 'DELETE',
      headers: {
        'Content-Type': 'application/json',
      },
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error('API proxy error:', error);
    return NextResponse.json(
      { error: 'Failed to delete from backend' },
      { status: 500 }
    );
  }
}

// Handle PUT requests
export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const { path } = await params;
  const backendUrl = getBackendUrl(path);

  try {
    const body = await request.json();
    const response = await fetch(backendUrl, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error('API proxy error:', error);
    return NextResponse.json(
      { error: 'Failed to put to backend' },
      { status: 500 }
    );
  }
}
