# Auth Service

## Setup: RSA Key Pair for JWT Signing

Auth uses RS256 (RSA) for signing JWTs. You need to generate an RSA private key and store it in Azure Key Vault.

### 1. Generate the RSA private key

```bash
openssl genrsa -out jwt-private-key.pem 2048
```

### 2. Store it in Azure Key Vault

```bash
az keyvault secret set \
  --vault-name <your-keyvault-name> \
  --name rag-agent-jwt-private-key \
  --file jwt-private-key.pem
```

### 3. Delete the local key file

```bash
rm jwt-private-key.pem
```

### 4. Refresh the ExternalSecret

The ExternalSecret `auth-external-secret` will pull the key into the `auth-credentials` Kubernetes secret as `jwt-private-key`. To force a refresh:

```bash
kubectl annotate externalsecret auth-external-secret -n rag-agent-dev \
  force-sync=$(date +%s) --overwrite
```

### 5. Restart auth

```bash
kubectl rollout restart deployment auth -n rag-agent-dev
```
