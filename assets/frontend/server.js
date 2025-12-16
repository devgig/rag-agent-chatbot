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

const { createServer } = require('http');
const { parse } = require('url');
const next = require('next');
const { WebSocketServer, WebSocket } = require('ws');

const dev = process.env.NODE_ENV !== 'production';
const hostname = process.env.HOSTNAME || 'localhost';
const port = parseInt(process.env.PORT || '3000', 10);

// Backend WebSocket URL - uses internal Kubernetes DNS
const BACKEND_WS_URL = process.env.BACKEND_WS_URL || 'ws://multi-agent-backend.multi-agent-dev.svc.cluster.local:8000';

const app = next({ dev, hostname, port });
const handle = app.getRequestHandler();

app.prepare().then(() => {
  const server = createServer(async (req, res) => {
    try {
      const parsedUrl = parse(req.url, true);
      await handle(req, res, parsedUrl);
    } catch (err) {
      console.error('Error occurred handling', req.url, err);
      res.statusCode = 500;
      res.end('internal server error');
    }
  });

  // Create WebSocket server
  const wss = new WebSocketServer({ noServer: true });

  // Handle WebSocket upgrade requests
  server.on('upgrade', (request, socket, head) => {
    const { pathname } = parse(request.url || '');

    // Check if this is a WebSocket request to /api/ws/*
    if (pathname && pathname.startsWith('/api/ws/')) {
      // Extract the backend path (remove /api prefix)
      const backendPath = pathname.substring(4); // Remove '/api'
      const backendUrl = `${BACKEND_WS_URL}${backendPath}`;

      console.log(`[WebSocket] Proxying ${pathname} to ${backendUrl}`);

      // Accept the client connection IMMEDIATELY to prevent timeout
      wss.handleUpgrade(request, socket, head, (clientWs) => {
        console.log('[WebSocket] Client connection accepted');

        // Now create connection to backend
        const backendWs = new WebSocket(backendUrl);
        let backendConnected = false;

        // Handle backend connection open
        backendWs.on('open', () => {
          backendConnected = true;
          console.log(`[WebSocket] Connected to backend: ${backendUrl}`);
        });

        // Proxy messages from client to backend
        clientWs.on('message', (message) => {
          if (backendWs.readyState === WebSocket.OPEN) {
            backendWs.send(message);
          }
        });

        // Proxy messages from backend to client
        backendWs.on('message', (message) => {
          if (clientWs.readyState === WebSocket.OPEN) {
            clientWs.send(message);
          }
        });

        // Handle client close
        clientWs.on('close', (code, reason) => {
          console.log(`[WebSocket] Client closed: ${code} ${reason}`);
          if (backendWs.readyState === WebSocket.OPEN || backendWs.readyState === WebSocket.CONNECTING) {
            backendWs.close();
          }
        });

        // Handle client error
        clientWs.on('error', (error) => {
          console.error('[WebSocket] Client error:', error);
          if (backendWs.readyState === WebSocket.OPEN || backendWs.readyState === WebSocket.CONNECTING) {
            backendWs.close();
          }
        });

        // Handle backend close
        backendWs.on('close', (code, reason) => {
          console.log(`[WebSocket] Backend closed: ${code} ${reason}`);
          if (clientWs.readyState === WebSocket.OPEN) {
            clientWs.close();
          }
        });

        // Handle backend error
        backendWs.on('error', (error) => {
          console.error('[WebSocket] Backend error:', error);
          if (clientWs.readyState === WebSocket.OPEN) {
            clientWs.close(1011, 'Backend connection error');
          }
        });
      });
    } else {
      // Not a WebSocket upgrade request we handle
      socket.destroy();
    }
  });

  server.listen(port, (err) => {
    if (err) throw err;
    console.log(`> Ready on http://${hostname}:${port}`);
    console.log(`> WebSocket proxy enabled for /api/ws/* -> ${BACKEND_WS_URL}`);
  });
});
