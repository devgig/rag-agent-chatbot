# Milvus Configuration Fix

## Issue

The backend deployment sets `MILVUS_ADDRESS` environment variable but the application code doesn't use it.

**Current State:**
- Deployment YAML sets: `MILVUS_ADDRESS=milvus.milvus-system.svc.cluster.local:19530`
- Code uses hardcoded default: `http://milvus:19530`
- Result: Connection will fail because DNS name `milvus` doesn't exist in the backend namespace

## Required Code Changes

### 1. Update `assets/backend/main.py`

Add environment variable reading (around line 50, after the PostgreSQL variables):

```python
# Existing PostgreSQL config
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", 5432))
POSTGRES_DB = os.getenv("POSTGRES_DB", "chatbot")
POSTGRES_USER = os.getenv("POSTGRES_USER", "chatbot_user")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "chatbot_password")

# ADD THIS: Milvus configuration
MILVUS_ADDRESS = os.getenv("MILVUS_ADDRESS", "milvus:19530")
# Ensure it has http:// prefix for Milvus client
if not MILVUS_ADDRESS.startswith("http://") and not MILVUS_ADDRESS.startswith("https://"):
    MILVUS_URI = f"http://{MILVUS_ADDRESS}"
else:
    MILVUS_URI = MILVUS_ADDRESS
```

Update the vector store initialization (around line 61):

```python
# BEFORE:
vector_store = create_vector_store_with_config(config_manager)

# AFTER:
vector_store = create_vector_store_with_config(
    config_manager,
    uri=MILVUS_URI
)
```

### 2. Update `assets/backend/vector_store.py`

The function `create_vector_store_with_config` already accepts a `uri` parameter (line 368), so no changes needed there. The fix in `main.py` is sufficient.

## Testing the Fix

### 1. Build Updated Backend Image

```bash
cd assets/backend
docker build -t rag-agent-chatbot/backend:latest .
```

### 2. Deploy to Kubernetes

```bash
kubectl apply -k kustomize/backend/overlays/dev
```

### 3. Verify Connection

Check backend logs to ensure Milvus connection succeeds:

```bash
kubectl logs -n rag-agent-dev -l app=rag-agent-backend --tail=50 | grep -i milvus
```

You should see logs like:
```
{"message": "Milvus vector store initialized", "uri": "http://milvus.milvus-system.svc.cluster.local:19530", "collection": "context"}
```

### 4. Test Functionality

Test document ingestion to verify Milvus is working:

```bash
# Use the API to upload a document
curl -X POST http://<backend-url>/ingest \
  -F "files=@test.txt"
```

## Optional: Adding Authentication Support

If you want to add Milvus authentication support, see `MILVUS_SETUP.md` for detailed instructions on:

1. Enabling authentication in Milvus Helm chart
2. Creating Milvus users
3. Storing credentials in Azure Key Vault
4. Updating code to use credentials

## Summary

**Minimum Required Change:**
- Update `assets/backend/main.py` to read `MILVUS_ADDRESS` from environment variable and pass it to `create_vector_store_with_config()`

**Optional Enhancements:**
- Add authentication support (if Milvus is configured with auth)
- Add connection pooling and retry logic
- Add health checks for Milvus connectivity
