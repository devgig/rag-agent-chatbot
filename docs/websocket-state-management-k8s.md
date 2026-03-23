# WebSocket and Conversation State Management in Kubernetes

This document outlines the challenges of running a WebSocket-based chat application in Kubernetes and how each one is addressed in this project.

---

## The Core Problem

WebSockets are long-lived, stateful TCP connections. Kubernetes is designed around stateless, ephemeral pods. These two models are fundamentally at odds:

- Pods can be killed, restarted, or rescheduled at any time.
- Load balancers distribute new connections across pods, but an existing WebSocket is pinned to one pod.
- Horizontal autoscaling adds/removes pods, but existing connections cannot be migrated.
- Service meshes (Istio) and reverse proxies add protocol translation layers that can silently break WebSocket upgrades.

This creates a set of interlocking challenges around connection lifecycle, authentication, state persistence, scaling, and gateway configuration.

---

## 1. Connection Lifecycle and Pod Eviction

### Challenge

When a backend pod is terminated (rolling update, scale-down, node drain), every active WebSocket connection on that pod is severed instantly. The user sees a broken connection mid-conversation with no warning.

### How It's Handled

**Backend** (`kustomize/backend/base/deployment.yaml:25`):

```yaml
cluster-autoscaler.kubernetes.io/safe-to-evict: "false"
```

This annotation tells the cluster autoscaler not to evict the pod to reclaim resources. It does not prevent eviction during rolling updates or node drains, but reduces unnecessary churn.

**Rolling update strategy** (`deployment.yaml:13-15`):

```yaml
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxSurge: 1
    maxUnavailable: 0
```

`maxUnavailable: 0` ensures at least one pod is always running during deployments. New pods come up before old pods terminate, minimizing the window where connections drop.

**Frontend reconnection** (`QuerySection.tsx:209-411`):

The client implements exponential backoff reconnection:

```typescript
const maxReconnectAttempts = 5;
// On unexpected close:
const delay = Math.min(1000 * Math.pow(2, reconnectAttempts.current), 16000);
```

On reconnect, the client re-authenticates and receives full conversation history from PostgreSQL, so the user experience is restored without data loss.

---

## 2. Authentication Over WebSocket

### Challenge

Standard Kubernetes ingress and service mesh policies (Istio AuthorizationPolicy) validate JWTs on HTTP headers. WebSocket upgrade requests are HTTP initially, but once upgraded, subsequent frames have no HTTP headers. Putting the JWT in the WebSocket URL would expose it in server logs, proxy access logs, and browser history.

### How It's Handled

**First-message authentication pattern** (`main.py:158-210`):

The WebSocket endpoint is exempted from Istio JWT validation at the gateway level:

```yaml
# istio-authorization-policy.yaml:29-33
# Allow unauthenticated WebSocket upgrade
- to:
    - operation:
        paths:
          - /api/backend-svc/ws/*
```

Instead, the backend performs its own JWT validation. After the WebSocket is accepted, the client must send an auth message as the first frame:

```python
data = await asyncio.wait_for(
    websocket.receive_text(),
    timeout=WS_AUTH_TIMEOUT,  # 10 seconds
)
# Expect: {"type": "auth", "token": "<jwt>"}
user_email = verify_websocket_token(auth_msg["token"])
```

If authentication fails, the connection is closed with code `4001` before any chat messages are exchanged.

**Frontend** (`QuerySection.tsx:255-264`):

```typescript
ws.onopen = () => {
  const token = getToken();
  ws.send(JSON.stringify({ type: "auth", token }));
};
```

**Token not in URL** (`api.ts:102-106`):

```typescript
/**
 * Token is NOT included in the URL — it is sent as the first message
 * after connection to avoid logging JWT in server/proxy access logs.
 */
```

---

## 3. Conversation State Persistence

### Challenge

Conversation state only exists in the LangGraph graph execution during a single query. If conversation history only lived in memory, a pod restart would erase all conversations.

### How It's Handled

**PostgreSQL-backed persistence**:

After each query completes, the full message history is persisted to PostgreSQL:

```python
# In _run_graph() finally block:
await self.conversation_store.save_messages(chat_id, self.last_state["messages"])
```

**Batched saves** (`postgres_storage.py:414-450`):

A background worker batches writes every 1 second to reduce database load:

```python
async def _batch_save_worker(self):
    while True:
        await asyncio.sleep(1.0)
        # ... flush pending saves in a single transaction
```

**Graceful shutdown** (`postgres_storage.py:193-226`):

On pod termination, pending saves are flushed before the connection pool closes:

```python
async def close(self):
    # Flush remaining pending saves before closing
    if self._pending_saves and self.pool:
        async with self._save_lock:
            saves = self._pending_saves.copy()
            self._pending_saves.clear()
        # ... write to PostgreSQL
```

**History reload on reconnect** (`main.py:212-214`):

When a client connects (or reconnects), the full conversation history is loaded from PostgreSQL and sent to the client:

```python
history_messages = await postgres_storage.get_messages(chat_id)
history = [postgres_storage._message_to_dict(msg) for msg in history_messages[1:]]
await websocket.send_json({"type": "history", "messages": history})
```

This means any pod can serve any user's conversation. No session affinity is needed.

---

## 4. Scaling WebSocket Connections with KEDA

### Challenge

WebSocket connections are long-lived. Traditional CPU/memory-based autoscaling doesn't account for connection count. A pod could be at low CPU but holding hundreds of WebSocket connections. Scaling down kills connections. Scaling up doesn't rebalance existing connections.

### How It's Handled

**KEDA ScaledObject** (`keda-scaledobject.yaml`):

```yaml
minReplicaCount: 1
maxReplicaCount: 5
pollingInterval: 15
cooldownPeriod: 60

triggers:
  - type: cpu
    metadata:
      value: "70"
  - type: memory
    metadata:
      value: "80"
```

**Conservative scale-down** to protect active connections:

```yaml
scaleDown:
  stabilizationWindowSeconds: 120  # Wait 2 min before scaling down
  policies:
    - type: Percent
      value: 25          # Remove at most 25% of pods per minute
      periodSeconds: 60
```

**Aggressive scale-up** to handle traffic spikes:

```yaml
scaleUp:
  stabilizationWindowSeconds: 0  # Scale up immediately
  policies:
    - type: Pods
      value: 2            # Add up to 2 pods per 30s
      periodSeconds: 30
```

**Limitation**: Scaling still uses CPU/memory metrics, not WebSocket connection count. A Prometheus-based trigger using connection metrics is prepared but commented out. The current approach works because the application is compute-bound (LLM inference drives CPU).

---

## 5. Per-User Connection Limiting

### Challenge

A misbehaving client or open browser tabs can exhaust server resources by opening many WebSocket connections. Without limits, a single user could consume all available file descriptors or memory on a pod.

### How It's Handled

**Server-side tracking** (`main.py:64-65`):

```python
_ws_connections: Dict[str, Set[str]] = defaultdict(set)  # email -> connection IDs
```

**Connection limit enforcement** (`main.py:202-205`):

```python
MAX_WS_CONNECTIONS_PER_USER = int(os.getenv("MAX_WS_CONNECTIONS_PER_USER", "5"))

if len(_ws_connections[user_email]) >= MAX_WS_CONNECTIONS_PER_USER:
    await websocket.close(code=4029, reason="Too many connections")
    return
```

**Cleanup on disconnect** (`main.py:239-241`):

```python
finally:
    if user_email:
        _ws_connections[user_email].discard(conn_id)
```

**Limitation**: The connection tracking dict is per-pod. With multiple replicas, a user could have up to `MAX_WS_CONNECTIONS_PER_USER * replica_count` total connections. This is acceptable at current scale but would need Redis-backed tracking for large deployments.

---

## 6. Istio Service Mesh and WebSocket Upgrades

### Challenge

Istio's ambient mesh intercepts traffic at L4/L7. WebSocket requires an HTTP `Upgrade` header that can be mishandled by:
- L7 policies that inspect HTTP headers but don't understand the WebSocket upgrade.
- Request authentication that rejects the upgrade request before it reaches the backend.
- URL rewriting that strips or mangles the WebSocket path.

### How It's Handled

**Gateway HTTPRoute** (`httproute.yaml`):

WebSocket paths are routed through the same HTTPRoute as regular API traffic. The Istio gateway handles WebSocket upgrades natively for HTTP/1.1 connections:

```yaml
rules:
  - matches:
      - path:
          type: PathPrefix
          value: /api/backend-svc
    filters:
      - type: URLRewrite
        urlRewrite:
          path:
            type: ReplacePrefixMatch
            replacePrefixMatch: /
    backendRefs:
      - name: rag-agent-backend
        port: 8000
```

The path `/api/backend-svc/ws/chat/{id}` is rewritten to `/ws/chat/{id}` before reaching the backend.

**Authorization bypass for WebSocket** (`istio-authorization-policy.yaml:29-33`):

WebSocket upgrade requests are explicitly allowed without JWT:

```yaml
- to:
    - operation:
        paths:
          - /api/backend-svc/ws/*
```

This is necessary because the browser's `WebSocket` API does not support custom HTTP headers on the upgrade request. JWT validation is handled by the backend's first-message auth instead.

**Frontend bypasses Istio sidecar** (`frontend/deployment.yaml`):

```yaml
annotations:
  istio.io/dataplane-mode: none
  ambient.istio.io/redirection: disabled
```

The frontend serves static files via nginx and does not need mTLS or traffic management.

---

## 7. Message Streaming During Active Queries

### Challenge

While the LangGraph agent is executing (retrieval + generation), tokens are streamed back in real time. If the WebSocket drops mid-stream, the partially generated response could be lost. The client and server need to stay in sync about which messages have been committed.

### How It's Handled

**Streaming via async queue** (`agent.py:510-524`):

Tokens are pushed through an `asyncio.Queue` and yielded to the WebSocket handler:

```python
token_q: asyncio.Queue[Any] = asyncio.Queue()
self.stream_callback = lambda event: self._queue_writer(event, token_q)
runner = asyncio.create_task(self._run_graph(...))

while True:
    item = await token_q.get()
    if item is SENTINEL:
        break
    yield item
```

**Post-completion sync** (`main.py:231-233`):

After the agent finishes, the backend sends the authoritative message history from PostgreSQL:

```python
final_messages = await postgres_storage.get_messages(chat_id)
final_history = [postgres_storage._message_to_dict(msg) for msg in final_messages[1:]]
await websocket.send_json({"type": "history", "messages": final_history})
```

This means the streamed tokens are a real-time preview, and the `history` event is the source of truth. If the connection drops mid-stream, the client reconnects and receives the complete history.

**Client-side batching** (`QuerySection.tsx:205-207, 326-350`):

Tokens are batched per animation frame to prevent UI jank:

```typescript
pendingTokens.current += text;
if (!rafId.current) {
  rafId.current = requestAnimationFrame(() => {
    // Flush batched tokens to React state
  });
}
```

---

## 8. LRU Cache Consistency

### Challenge

The PostgreSQL storage layer uses an LRU cache to reduce database reads (`postgres_storage.py:46-91`). In a multi-replica deployment, pod A might cache stale data while pod B has written newer messages. Cache invalidation is local to each pod.

### How It's Handled

**Current approach**: The cache has a 300-second TTL (`cache_ttl=300`). After 5 minutes, entries expire and are re-fetched from PostgreSQL. A background worker evicts expired entries every 60 seconds:

```python
async def _cache_eviction_worker(self):
    while True:
        await asyncio.sleep(60)
        msg_evicted = self._message_cache.evict_expired()
        meta_evicted = self._metadata_cache.evict_expired()
```

**Write-through on the active pod**: When the active pod saves messages, it updates both PostgreSQL and the local cache simultaneously, so the pod serving the WebSocket always has fresh data:

```python
async def save_messages(self, chat_id, messages):
    async with self._save_lock:
        self._pending_saves[chat_id] = messages.copy()
    self._cache_messages(chat_id, messages)  # Update local cache immediately
```

**Limitation**: If pod A writes a message and the user reconnects to pod B, pod B's cache may serve stale data for up to 5 minutes. In practice, this is mitigated because:
1. Only 1 replica runs by default (KEDA `minReplicaCount: 1`).
2. Cache misses always fall through to PostgreSQL.
3. The `history` event sent on connect always reads from the database on a cache miss.

---

## 9. Message Size and Input Validation

### Challenge

WebSocket frames have no built-in size limits. A malicious client could send arbitrarily large messages, consuming server memory.

### How It's Handled

**Server-side limit** (`main.py:59, 218-220`):

```python
MAX_WS_MESSAGE_BYTES = int(os.getenv("MAX_WS_MESSAGE_BYTES", str(64 * 1024)))  # 64KB

while True:
    data = await websocket.receive_text()
    if len(data.encode("utf-8")) > MAX_WS_MESSAGE_BYTES:
        await websocket.send_json({"type": "error", "content": "Message too large"})
        continue
```

The connection stays open and the oversized message is rejected without termination.

---

## 10. Health Checks vs. WebSocket Liveness

### Challenge

Kubernetes health probes use HTTP GET requests. They verify the HTTP server is responding but say nothing about whether WebSocket connections are functional, whether the agent is initialized, or whether downstream services (PostgreSQL, Milvus) are reachable.

### How It's Handled

**Current probes** (`deployment.yaml:109-136`):

All three probes (startup, liveness, readiness) check the `/health` endpoint:

```python
@app.get("/health")
async def health_check():
    return {"status": "healthy"}
```

**Startup probe** allows slow initialization:

```yaml
startupProbe:
  initialDelaySeconds: 10
  periodSeconds: 10
  failureThreshold: 30  # Up to 5 minutes to start
```

**Limitation**: The health check does not verify:
- WebSocket connection count or saturation
- PostgreSQL pool availability
- Milvus connectivity

A more comprehensive health check could report on these subsystems, but the current approach avoids probe failures due to transient downstream issues.

---

## Architecture Summary

```
Browser (WebSocket Client)
    │
    │  ws://sparkchat.bytecourier.local/api/backend-svc/ws/chat/{id}
    │
    ▼
Istio Gateway (L7)
    │  - URL rewrite: strip /api/backend-svc prefix
    │  - AuthZ: allow /ws/* without JWT
    │
    ▼
K8s Service (ClusterIP, no session affinity)
    │
    ▼
Backend Pod (FastAPI + uvicorn)
    │  - First-message JWT auth
    │  - Per-user connection limit
    │  - Async streaming via Queue
    │
    ├──► LangGraph (START → generate → END)
    │       Inline vector search + LLM call
    │
    ├──► PostgreSQL (asyncpg pool)
    │       Durable conversation storage
    │       LRU cache with 300s TTL
    │       Batched writes (1s interval)
    │
    └──► Milvus (vector search)
            Direct query via VectorStore
```

---

## Key Design Decisions

| Decision | Rationale |
| --- | --- |
| First-message auth instead of URL token | Prevents JWT leaking into logs and browser history |
| PostgreSQL for state, not in-memory | Survives pod restarts; any pod can serve any user |
| No session affinity | Avoids hot-spotting; stateless pod design |
| Batched saves with flush-on-shutdown | Reduces DB write pressure while preventing data loss |
| Conservative scale-down (2 min, 25%) | Minimizes connection disruption during autoscaling |
| `safe-to-evict: false` annotation | Prevents unnecessary pod eviction by cluster autoscaler |
| History event after every query | Client always gets authoritative state from PostgreSQL |
| Exponential backoff reconnection | Handles transient failures without thundering herd |
| 64KB message size limit | Prevents memory exhaustion from oversized messages |

---

## Challenges and Solutions Summary

| # | Challenge | Root Cause | Solution | Key Files |
| --- | --- | --- | --- | --- |
| 1 | **Pod eviction severs WebSocket connections** | K8s pods are ephemeral; rolling updates and scale-down kill active connections | `safe-to-evict: false` annotation, `maxUnavailable: 0` rolling update, client exponential backoff reconnection (5 attempts, up to 16s delay) | `deployment.yaml:25`, `QuerySection.tsx:401-410` |
| 2 | **JWT auth on WebSocket upgrade** | Browser `WebSocket` API cannot set custom HTTP headers; putting JWT in URL leaks it to logs | First-message auth: Istio allows `/ws/*` unauthenticated, backend validates JWT sent as first WebSocket frame within 10s timeout | `main.py:158-210`, `istio-authorization-policy.yaml:29-33`, `api.ts:102-106` |
| 3 | **Conversation state lost on pod restart** | LangGraph state is ephemeral during query execution | PostgreSQL for durable persistence; batched writes every 1s with flush-on-shutdown | `agent.py`, `postgres_storage.py:414-450,193-226` |
| 4 | **Autoscaling disrupts active connections** | Scale-down terminates pods holding WebSocket connections; scale-up doesn't rebalance existing connections | KEDA with conservative scale-down: 2 min stabilization, 25% max reduction per minute; aggressive scale-up: immediate, 2 pods per 30s | `keda-scaledobject.yaml` |
| 5 | **Single user exhausts connections** | No built-in WebSocket connection limit; open tabs accumulate | Per-user server-side tracking (`email -> Set[conn_id]`), configurable limit (default 5), close with code `4029` on excess | `main.py:64-65,202-205` |
| 6 | **Istio breaks WebSocket upgrades** | Service mesh L7 policies, request authentication, and URL rewriting can interfere with upgrade handshake | Explicit AuthorizationPolicy bypass for `/ws/*`, HTTPRoute URL rewrite strips `/api/backend-svc` prefix, frontend bypasses Istio sidecar entirely | `httproute.yaml`, `istio-authorization-policy.yaml`, `frontend/deployment.yaml` |
| 7 | **Partial response lost on mid-stream disconnect** | Tokens are streamed in real time; connection drop loses uncommitted content | Streamed tokens are a preview only; after query completes, backend sends authoritative `history` event from PostgreSQL; client replaces local state on reconnect | `main.py:231-233`, `agent.py:510-524` |
| 8 | **Stale LRU cache across replicas** | Each pod has its own LRU cache; pod B may serve stale data after pod A writes | 300s TTL with background eviction every 60s; write-through on active pod; cache miss always falls through to PostgreSQL; mitigated by single-replica default | `postgres_storage.py:46-91,339-341,536-548` |
| 9 | **Oversized WebSocket messages** | No built-in frame size limit; malicious client could send arbitrarily large payloads | Server-side 64KB limit; oversized messages rejected with error, connection stays open | `main.py:59,218-220` |
| 10 | **Health probes don't verify WebSocket** | K8s probes are HTTP GET only; they confirm HTTP server is up but not WebSocket functionality or downstream health | Accepted trade-off: `/health` endpoint checks HTTP liveness; startup probe allows 5 min for slow init; downstream failures surface as query errors, not probe failures | `main.py:142-145`, `deployment.yaml:109-136` |
