# PostgreSQL Setup Guide

This project uses PostgreSQL for storing chat conversations and document source metadata.

## Deployment Options

### Option 1: Helm Chart (Kubernetes)

Deploy using the Bitnami Helm chart:

```yaml
helmCharts:
  - name: postgresql
    repo: https://charts.bitnami.com/bitnami
    version: "13.2.24"
    releaseName: postgresql
    namespace: postgres-system
    valuesFile: values.yaml
```

### Option 2: Docker (Local Development)

```bash
docker run -d --name postgres \
  -e POSTGRES_DB=chatbot \
  -e POSTGRES_USER=chatbot_user \
  -e POSTGRES_PASSWORD=your-secure-password-here \
  -p 5432:5432 postgres:15
```

## Database Setup

### 1. Connect to PostgreSQL

```bash
# Kubernetes
kubectl exec -it -n postgres-system postgresql-0 -- psql -U postgres

# Docker / Local
psql -U postgres -h localhost
```

### 2. Create Database and User

```sql
-- Create the chatbot database
CREATE DATABASE chatbot;

-- Create the chatbot user
CREATE USER chatbot_user WITH PASSWORD '<your-secure-password>';

-- Grant privileges to the user
GRANT ALL PRIVILEGES ON DATABASE chatbot TO chatbot_user;

-- Connect to the chatbot database
\c chatbot

-- Grant schema privileges
GRANT ALL ON SCHEMA public TO chatbot_user;
```

### 3. Configure Credentials

Store credentials securely using your preferred secrets management solution (e.g., Kubernetes Secrets, External Secrets Operator, environment variables).

The backend expects these environment variables:

| Variable | Description |
|----------|-------------|
| `POSTGRES_HOST` | PostgreSQL hostname |
| `POSTGRES_DB` | Database name (`chatbot`) |
| `POSTGRES_USER` | Database user (`chatbot_user`) |
| `POSTGRES_PASSWORD` | Database password |

See `kustomize/backend/base/deployment.yaml` for the Kubernetes deployment configuration.

## Database Schema

The application automatically creates the necessary tables and indexes on first run. Ensure the database user has sufficient privileges to create tables.

## Troubleshooting

### Test Database Connection

```bash
# From within the cluster
kubectl run -it --rm psql-test --image=postgres:15-alpine --restart=Never -- \
  psql -h <postgres-host> -U chatbot_user -d chatbot

# Local
psql -h localhost -U chatbot_user -d chatbot
```

### Check Backend Logs

```bash
kubectl logs -n rag-agent -l app=rag-agent-backend --tail=50
```

## Security Notes

- Never commit database passwords to version control
- Use strong, randomly generated passwords
- Rotate passwords regularly
- Limit database user privileges to only what's necessary
