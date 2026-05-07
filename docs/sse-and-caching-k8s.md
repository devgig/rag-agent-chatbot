# Streaming Chat over SSE with a Two-Tier Cache in Kubernetes

This document explains how the chat backend streams LLM responses, why we
moved from WebSocket to Server-Sent Events, and how the L1/L2 cache plus
Istio session affinity work together.

---

## The Core Problem

LLM responses are streamed token-by-token. The transport needs to:

- Push tokens to the browser as they arrive (low latency, no buffering)
- Survive Kubernetes' usual disruptions (rolling deploys, KEDA scale events)
- Authenticate cleanly through an Istio gateway with `AuthorizationPolicy`
- Let any backend pod handle any user request, while still keeping a hot
  in-process cache when affinity allows

The previous WebSocket design satisfied #1 well but pinned each chat
session to a single pod for its full lifetime. Rolling deploys dropped
mid-conversation users; KEDA scale-up didn't help existing connections.

---

## Why SSE Instead of WebSocket

Chat traffic in this app is overwhelmingly server→client streaming. The
client sends one JSON body per query; the server streams many tokens
back. SSE is a better fit for that shape:

| | WebSocket (old) | SSE (current) |
|---|---|---|
| Pinned to a pod | Whole chat session | Just the in-flight response |
| Auth | In-band first message | Standard `Authorization: Bearer` header |
| Istio policy | `/ws/*` bypass needed | Plain HTTP — no special case |
| Reconnect | Custom logic + exponential backoff | Cancel & re-POST a new query |
| Headers / cookies | Browser API can't set them on upgrade | Normal `fetch()` rules apply |
| Binary frames | Yes | No (UTF-8 only — fine for tokens) |

Tradeoff: SSE is one-way. Anything the client wants to push at the server
goes through a separate POST/REST call. For this app that's only the
chat message itself, so it costs nothing.

---

## Endpoints

```
GET  /chat/{chat_id}/history
     → { "messages": [...], "partial": { "content": "...", "truncated": true } | null }

POST /chat/{chat_id}/query     body: { "message": "..." }
     → text/event-stream
        data: {"type":"node_start","data":"generate"}
        data: {"type":"token","data":"Hello"}
        data: {"type":"token","data":" world"}
        data: {"type":"usage","data":{...}}
        data: {"type":"history","messages":[...]}
        data: {"type":"done"}
```

Auth is the standard JWT bearer. The Istio `AuthorizationPolicy` enforces
JWT presence at the gateway; no path bypass is required.

---

## Cache Tiers

The cache pairs a per-pod in-memory layer with a shared Redis layer.
Lookups try L1 → L2 → PostgreSQL; writes go through L1 and L2
synchronously, with PostgreSQL persistence batched in the background.

### L1: in-process LRU (per pod)

`postgres_storage.py` already had this — a bounded `LRUCache` with a
300-second TTL keyed by `chat_id`. With Istio session affinity (see
below), a returning user lands on the same pod and hits L1.

### L2: Redis (shared across pods)

`cache.py` holds a thin async Redis adapter. It connects to the cluster's
existing `redis.redis-system.svc.cluster.local:6379` (Bitnami HA Redis
with Sentinel) and uses `redis-credentials` Secret keys (`host`, `port`,
`password`) projected from Azure Key Vault via `ExternalSecret`.

L2 covers the cases L1 can't:
- A new pod (KEDA scale-up) has an empty L1
- A pod restart (rolling deploy) loses its L1
- Affinity hashes a user to a different pod (replica scale change)

When L1 misses but L2 hits, L1 is warmed so subsequent reads on that pod
skip Redis entirely.

### Default TTL

Set by `CACHE_L2_TTL_SECONDS` env var, default `28800` (8 hours).

---

## Session Affinity

```yaml
# kustomize/backend/base/destinationrule.yaml
trafficPolicy:
  loadBalancer:
    consistentHash:
      httpHeaderName: authorization
```

Istio hashes on the JWT in the `Authorization` header so a given user's
requests land on the same backend pod whenever possible. This maximizes
L1 hit rate without requiring cookies. KEDA can still scale the
deployment freely; new pods take traffic from new users until consistent
hashing reshuffles.

Service-level `sessionAffinity: ClientIP` is intentionally not used —
behind a gateway, every request looks like it came from the gateway's IP,
so all users would land on one pod.

---

## Cancellation and Partial Responses

When a user clicks "stop" or closes the tab, the frontend calls
`AbortController.abort()`. The backend detects the disconnect mid-stream
and persists the buffered token prefix to Redis with the L2 TTL:

```
Key:   sparkchat:partial:{chat_id}
Value: { "content": "...", "truncated": true }
TTL:   8 hours
```

On next history load the partial is returned alongside committed history,
and the frontend renders it with a `(stopped)` marker. The partial is
cleared automatically when the user sends a new message (which would
overwrite it anyway), or by TTL expiry.

Partial responses do *not* go to PostgreSQL — only completed responses
become durable conversation history.

---

## Conversation State Persistence

Conversation history lives in PostgreSQL (`conversations` table, JSONB
column). After each successful query the agent writes the full message
list; the SSE handler then emits a final `history` event so the client
can replace its local optimistic state with the authoritative version.

A background `_batch_save_worker` flushes pending writes every second and
also write-throughs to L2 so peer pods see fresh data on the next miss.
On pod shutdown, pending saves flush before the connection pool closes.

---

## Per-User Concurrency Limit

`MAX_STREAMS_PER_USER` (default 5) caps how many SSE streams one user
(identified by JWT subject) can run concurrently against a single pod.
With session affinity, that's effectively a per-user cap. Excess
requests get HTTP 429.

---

## Health Checks

`/health` is a plain HTTP GET that returns `200 OK`. Probes don't verify
PostgreSQL, Milvus, or Redis connectivity — those failures surface as
query errors rather than probe failures, on the principle that one
flaky downstream shouldn't restart the pod.

---

## Architecture Summary

```
Browser
    │
    │  POST /api/backend-svc/chat/{id}/query           (SSE stream)
    │  GET  /api/backend-svc/chat/{id}/history         (JSON)
    │  Authorization: Bearer <jwt>
    │
    ▼
Istio Gateway (L7) — JWT enforced via AuthorizationPolicy
    │  URL rewrite: strip /api/backend-svc prefix
    │  HTTPRoute timeouts: request 600s, backendRequest 600s
    │
    ▼
Istio DestinationRule — consistentHash on Authorization → same pod for same user
    │
    ▼
Backend Pod (FastAPI + uvicorn)
    │  StreamingResponse(media_type="text/event-stream")
    │  Per-user concurrent stream cap
    │
    ├──► PostgreSQL (durable conversation store)
    │       L1: in-process LRU (300s)
    │       Source of truth on miss
    │
    ├──► Redis (shared L2 + partial-response store)
    │       L2 messages: 8h TTL, write-through on save
    │       Partials:    8h TTL, written on cancel only
    │
    ├──► Milvus (vector search)
    └──► vLLM model server (token stream)
```

---

## Key Design Decisions

| Decision | Rationale |
| --- | --- |
| SSE instead of WebSocket | Chat is server-push dominant; SSE rides plain HTTP, simplifies auth and gateway config |
| `consistentHash` on `authorization` header | Same user → same pod → warm L1, without cookies |
| L1 + L2 (Redis) cache, both 8h TTL | L1 fast path for affinitized user; L2 catches misses on cold pod or after rebalance |
| Partial response in Redis with TTL | Auto-cleanup; doesn't pollute Postgres history with truncated drafts |
| Cancel via `AbortController` | Standard browser API; backend detects disconnect and persists partial |
| `request: 600s` HTTPRoute timeout | Default 15s would cut off long LLM generations |
| L2 fallback is soft-fail | Redis outage degrades to direct Postgres reads; chat keeps working |

---

## Code Map

| Concern | File |
|---|---|
| SSE endpoints | `assets/backend/main.py` |
| Redis L2 + partials | `assets/backend/cache.py` |
| L1 + L2 wiring on hot reads | `assets/backend/postgres_storage.py` |
| Frontend SSE client | `assets/frontend/src/lib/api.ts` (`streamChatQuery`) |
| Frontend chat lifecycle | `assets/frontend/src/components/QuerySection.tsx` |
| Affinity | `kustomize/backend/base/destinationrule.yaml` |
| Redis credentials | `kustomize/backend/base/redis-external-secret.yaml` |
| Stream timeouts | `kustomize/backend/base/httproute.yaml` |
| JWT enforcement | `kustomize/gateway/base/istio-authorization-policy.yaml` |
