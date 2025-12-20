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

  // Create WebSocket server with compression disabled
  const wss = new WebSocketServer({
    noServer: true,
    perMessageDeflate: false
  });

  // Handle WebSocket upgrade requests
  server.on('upgrade', (request, socket, head) => {
    const { pathname } = parse(request.url || '');

    // Check if this is a WebSocket request to /api/ws/*
    if (pathname && pathname.startsWith('/api/ws/')) {
      // Extract the backend path (remove /api prefix)
      const backendPath = pathname.substring(4);
      const backendUrl = `${BACKEND_WS_URL}${backendPath}`;
      const startTime = Date.now();

      console.log(`[WebSocket] Proxying ${pathname} to ${backendUrl}`);

      // Connect to backend FIRST, then accept client
      console.log(`[WebSocket] Starting backend connection...`);
      const backendWs = new WebSocket(backendUrl);

      // Set a timeout for backend connection
      const backendTimeout = setTimeout(() => {
        console.log(`[WebSocket] Backend connection timeout after 5000ms`);
        backendWs.terminate();
        socket.destroy();
      }, 5000);

      // Handle backend connection error during initial connect
      backendWs.once('error', (error) => {
        clearTimeout(backendTimeout);
        console.error('[WebSocket] Backend connection failed:', error.message);
        socket.destroy();
      });

      // When backend connects, accept the client
      backendWs.once('open', () => {
        clearTimeout(backendTimeout);
        console.log(`[WebSocket] Backend connected after ${Date.now() - startTime}ms, accepting client...`);

        // Now accept the client connection
        wss.handleUpgrade(request, socket, head, (clientWs) => {
          console.log(`[WebSocket] Client accepted after ${Date.now() - startTime}ms`);

          let pingInterval = setInterval(() => {
            if (clientWs.readyState === WebSocket.OPEN) {
              clientWs.ping();
            }
          }, 30000);

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
            clearInterval(pingInterval);
            console.log(`[WebSocket] Client closed: ${code} ${reason}`);
            if (backendWs.readyState === WebSocket.OPEN) {
              backendWs.close();
            }
          });

          // Handle client error
          clientWs.on('error', (error) => {
            console.error('[WebSocket] Client error:', error.message);
            if (backendWs.readyState === WebSocket.OPEN) {
              backendWs.close();
            }
          });

          // Handle backend close (after connection established)
          backendWs.on('close', (code, reason) => {
            clearInterval(pingInterval);
            console.log(`[WebSocket] Backend closed: ${code} ${reason}`);
            if (clientWs.readyState === WebSocket.OPEN) {
              clientWs.close();
            }
          });

          // Handle backend error (after connection established)
          backendWs.on('error', (error) => {
            console.error('[WebSocket] Backend error:', error.message);
            if (clientWs.readyState === WebSocket.OPEN) {
              clientWs.close(1011, 'Backend error');
            }
          });
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
