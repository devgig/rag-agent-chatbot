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

// Backend LoadBalancer IP (from kubectl get svc)
const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || 'http://192.168.71.206:8000';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const { path: pathArray } = await params;
  const path = pathArray.join('/');
  const url = `${BACKEND_URL}/${path}${request.nextUrl.search}`;

  try {
    const response = await fetch(url, {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json',
      },
      cache: 'no-store',
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error(`Error proxying GET /${path}:`, error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const { path: pathArray } = await params;
  const path = pathArray.join('/');
  const url = `${BACKEND_URL}/${path}${request.nextUrl.search}`;

  try {
    const contentType = request.headers.get('content-type') || '';
    let body: BodyInit | null = null;
    let headers: HeadersInit = {};

    // Handle multipart/form-data (file uploads)
    if (contentType.includes('multipart/form-data')) {
      // Get the FormData from the request
      const formData = await request.formData();
      body = formData as any; // FormData is a valid BodyInit
      // Don't set Content-Type header - fetch will set it with the boundary
    } else {
      // Handle JSON requests
      const jsonBody = await request.json().catch(() => null);
      if (jsonBody) {
        body = JSON.stringify(jsonBody);
        headers = { 'Content-Type': 'application/json' };
      }
    }

    const response = await fetch(url, {
      method: 'POST',
      headers,
      body: body || undefined,
      cache: 'no-store',
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error(`Error proxying POST /${path}:`, error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const { path: pathArray } = await params;
  const path = pathArray.join('/');
  const url = `${BACKEND_URL}/${path}${request.nextUrl.search}`;

  try {
    const response = await fetch(url, {
      method: 'DELETE',
      headers: {
        'Content-Type': 'application/json',
      },
      cache: 'no-store',
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error(`Error proxying DELETE /${path}:`, error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
