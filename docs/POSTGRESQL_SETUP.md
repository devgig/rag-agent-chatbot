# PostgreSQL Setup Guide

This project uses an existing PostgreSQL installation deployed via Helm chart in the `postgres-system` namespace.

## Helm Chart Configuration

The PostgreSQL instance is deployed using the Bitnami Helm chart:

```yaml
helmCharts:
  - name: postgresql
    repo: https://charts.bitnami.com/bitnami
    version: "13.2.24"
    releaseName: postgresql
    namespace: postgres-system
    valuesFile: values.yaml
```

## Database Setup

After the PostgreSQL Helm chart is deployed, you need to create the database and user for the multi-agent chatbot application.

### 1. Connect to PostgreSQL

Connect to the PostgreSQL pod:

```bash
kubectl exec -it -n postgres-system postgresql-0 -- bash
```

### 2. Create Database and User

Connect to PostgreSQL as the postgres superuser:

```bash
psql -U postgres
```

Run the following SQL commands:

```sql
-- Create the chatbot database
CREATE DATABASE chatbot;

-- Create the chatbot user
CREATE USER chatbot_user WITH PASSWORD 'your-secure-password-here';

-- Grant privileges to the user
GRANT ALL PRIVILEGES ON DATABASE chatbot TO chatbot_user;

-- Connect to the chatbot database
\c chatbot

-- Grant schema privileges
GRANT ALL ON SCHEMA public TO chatbot_user;

-- Exit psql
\q
```

### 3. Store Credentials in Azure Key Vault

The application uses External Secrets Operator to retrieve PostgreSQL credentials from Azure Key Vault.

Create the password secret in Azure Key Vault:

```bash
az keyvault secret set \
  --vault-name <your-keyvault-name> \
  --name multi-agent-postgresql-password \
  --value "your-secure-password-here"
```

### 4. Verify External Secret

The backend deployment includes an ExternalSecret resource that pulls the password from Azure Key Vault:

- **Secret Name**: `postgres-credentials`
- **Key Vault Secret**: `multi-agent-postgresql-password`
- **Database**: `chatbot`
- **Username**: `chatbot_user`

Verify the ExternalSecret is syncing correctly:

```bash
kubectl get externalsecret -n multi-agent-dev postgres-external-secret
kubectl get secret -n multi-agent-dev postgres-credentials
```

## Connection Details

The backend application connects to PostgreSQL using:

- **Host**: `postgresql.postgres-system.svc.cluster.local`
- **Database**: `chatbot` (from secret)
- **Username**: `chatbot_user` (from secret)
- **Password**: Retrieved from Azure Key Vault via ExternalSecret
- **Port**: 5432 (default)

## Database Schema

The application will automatically create the necessary database schema on first run. Ensure the `chatbot_user` has sufficient privileges to create tables and indexes.

## Troubleshooting

### Test Database Connection

From within a pod in the cluster:

```bash
kubectl run -it --rm psql-test --image=postgres:15-alpine --restart=Never -- \
  psql -h postgresql.postgres-system.svc.cluster.local -U chatbot_user -d chatbot
```

### Check External Secret Status

```bash
# Check ExternalSecret
kubectl describe externalsecret -n multi-agent-dev postgres-external-secret

# Check if secret was created
kubectl get secret -n multi-agent-dev postgres-credentials -o yaml
```

### Check Backend Logs

```bash
kubectl logs -n multi-agent-dev -l app=multi-agent-backend --tail=50
```

## Security Notes

- Never commit database passwords to version control
- Use strong, randomly generated passwords
- Rotate passwords regularly
- Ensure the ClusterSecretStore is properly configured to access Azure Key Vault
- Limit database user privileges to only what's necessary for the application
