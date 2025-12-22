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
    // Skip Next.js for WebSocket upgrade requests - they're handled by 'upgrade' event
    if (req.headers.upgrade && req.headers.upgrade.toLowerCase() === 'websocket') {
      return; // Don't respond - let upgrade event handle it
    }
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

      // Listen for socket errors before upgrade
      socket.on('error', (err) => {
        console.error(`[WebSocket] Raw socket error:`, err.message);
      });
      socket.on('close', () => {
        console.log(`[WebSocket] Raw socket closed`);
      });
      socket.on('end', () => {
        console.log(`[WebSocket] Raw socket ended`);
      });

      console.log(`[WebSocket] Socket before upgrade: destroyed=${socket.destroyed}, writableEnded=${socket.writableEnded}`);

      // Accept client IMMEDIATELY to avoid LoadBalancer timeout
      // Then connect to backend in parallel
      wss.handleUpgrade(request, socket, head, (clientWs) => {
        console.log(`[WebSocket] Client accepted after ${Date.now() - startTime}ms, readyState: ${clientWs.readyState}`);
        console.log(`[WebSocket] Socket after upgrade: destroyed=${socket.destroyed}, writableEnded=${socket.writableEnded}`);

        let backendWs = null;
        let backendReady = false;
        let messageQueue = [];
        let clientClosed = false;

        // Start backend connection immediately after accepting client
        console.log(`[WebSocket] Starting backend connection to ${backendUrl}...`);
        try {
          backendWs = new WebSocket(backendUrl);
          console.log(`[WebSocket] Backend WebSocket created`);
        } catch (err) {
          console.error(`[WebSocket] Error creating backend WebSocket:`, err.message);
          clientWs.close(1011, 'Backend connection error');
          return;
        }

        // Set a timeout for backend connection
        const backendTimeout = setTimeout(() => {
          console.log(`[WebSocket] Backend connection timeout after 5000ms`);
          if (backendWs) backendWs.terminate();
          if (clientWs.readyState === WebSocket.OPEN) {
            clientWs.close(1011, 'Backend connection timeout');
          }
        }, 5000);

        let pingInterval = setInterval(() => {
          if (clientWs.readyState === WebSocket.OPEN) {
            clientWs.ping();
          }
        }, 30000);

        // Handle backend connection error
        backendWs.on('error', (error) => {
          console.error('[WebSocket] Backend error:', error.message);
          if (!backendReady) {
            clearTimeout(backendTimeout);
            if (clientWs.readyState === WebSocket.OPEN) {
              clientWs.close(1011, 'Backend connection failed');
            }
          } else {
            if (clientWs.readyState === WebSocket.OPEN) {
              clientWs.close(1011, 'Backend error');
            }
          }
        });

        // When backend connects, flush queued messages
        backendWs.on('open', () => {
          clearTimeout(backendTimeout);
          backendReady = true;
          console.log(`[WebSocket] Backend connected after ${Date.now() - startTime}ms`);

          // If client already closed, close backend too
          if (clientClosed) {
            console.log(`[WebSocket] Client already closed, closing backend`);
            backendWs.close();
            return;
          }

          // Flush any queued messages
          if (messageQueue.length > 0) {
            console.log(`[WebSocket] Flushing ${messageQueue.length} queued messages`);
            messageQueue.forEach(msg => backendWs.send(msg));
            messageQueue = [];
          }
        });

        // Proxy messages from backend to client
        backendWs.on('message', (message) => {
          if (clientWs.readyState === WebSocket.OPEN) {
            clientWs.send(message);
          }
        });

        // Handle backend close
        backendWs.on('close', (code, reason) => {
          console.log(`[WebSocket] Backend closed: ${code} ${reason}`);
          clearInterval(pingInterval);
          if (clientWs.readyState === WebSocket.OPEN) {
            clientWs.close();
          }
        });

        // Proxy messages from client to backend (queue if backend not ready)
        clientWs.on('message', (message) => {
          if (backendReady && backendWs.readyState === WebSocket.OPEN) {
            backendWs.send(message);
          } else if (!backendReady) {
            // Queue message until backend is ready
            console.log(`[WebSocket] Queueing message (backend not ready)`);
            messageQueue.push(message);
          }
        });

        // Handle client close
        clientWs.on('close', (code, reason) => {
          clientClosed = true;
          clearInterval(pingInterval);
          clearTimeout(backendTimeout);
          console.log(`[WebSocket] Client closed: ${code} ${reason}`);
          if (backendWs && backendWs.readyState === WebSocket.OPEN) {
            backendWs.close();
          } else if (backendWs && backendWs.readyState === WebSocket.CONNECTING) {
            backendWs.terminate();
          }
        });

        // Handle client error
        clientWs.on('error', (error) => {
          console.error('[WebSocket] Client error:', error.message);
          if (backendWs && backendWs.readyState === WebSocket.OPEN) {
            backendWs.close();
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
