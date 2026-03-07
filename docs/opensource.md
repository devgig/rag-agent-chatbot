# Open Source Software Notice

This document identifies all open source software used in the **RAG Agent Chatbot** project.

**Project License:** Apache-2.0
**Copyright:** NVIDIA CORPORATION & AFFILIATES, 1993-2025

---

## Table of Contents

- [Frontend Dependencies (JavaScript/TypeScript)](#frontend-dependencies-javascripttypescript)
- [Backend Dependencies (Python)](#backend-dependencies-python)
- [Infrastructure & Container Images](#infrastructure--container-images)
- [AI Models](#ai-models)
- [Build Tools & Package Managers](#build-tools--package-managers)

---

## Frontend Dependencies (JavaScript/TypeScript)

Source: `assets/frontend/package.json`

### Runtime Dependencies

| Package | Version | License | Description |
|---------|---------|---------|-------------|
| [React](https://github.com/facebook/react) | ^19.0.0 | MIT | UI component library |
| [React DOM](https://github.com/facebook/react) | ^19.0.0 | MIT | React rendering for the browser |
| [react-markdown](https://github.com/remarkjs/react-markdown) | ^10.1.0 | MIT | Markdown renderer for React |
| [react-syntax-highlighter](https://github.com/react-syntax-highlighter/react-syntax-highlighter) | ^15.6.1 | MIT | Syntax highlighting for code blocks |
| [remark-gfm](https://github.com/remarkjs/remark-gfm) | ^4.0.1 | MIT | GitHub Flavored Markdown support |

### Development Dependencies

| Package | Version | License | Description |
|---------|---------|---------|-------------|
| [@types/node](https://github.com/DefinitelyTyped/DefinitelyTyped) | ^22 | MIT | TypeScript type definitions for Node.js |
| [@types/react](https://github.com/DefinitelyTyped/DefinitelyTyped) | ^19 | MIT | TypeScript type definitions for React |
| [@types/react-dom](https://github.com/DefinitelyTyped/DefinitelyTyped) | ^19 | MIT | TypeScript type definitions for React DOM |
| [@types/react-syntax-highlighter](https://github.com/DefinitelyTyped/DefinitelyTyped) | ^15.5.13 | MIT | TypeScript type definitions for react-syntax-highlighter |
| [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react) | ^4.3.4 | MIT | Vite plugin for React support |
| [autoprefixer](https://github.com/postcss/autoprefixer) | ^10.4.22 | MIT | PostCSS plugin to add vendor prefixes |
| [ESLint](https://github.com/eslint/eslint) | ^9 | MIT | JavaScript/TypeScript linter |
| [PostCSS](https://github.com/postcss/postcss) | ^8 | MIT | CSS transformation tool |
| [Tailwind CSS](https://github.com/tailwindlabs/tailwindcss) | ^3.4.1 | MIT | Utility-first CSS framework |
| [TypeScript](https://github.com/microsoft/TypeScript) | ^5 | Apache-2.0 | Typed superset of JavaScript |
| [Vite](https://github.com/vitejs/vite) | ^6.0.7 | MIT | Frontend build tool and dev server |

---

## Backend Dependencies (Python)

Source: `assets/backend/pyproject.toml`

### Direct Dependencies

| Package | Version | License | Description |
|---------|---------|---------|-------------|
| [FastAPI](https://github.com/fastapi/fastapi) | >=0.116.1 | MIT | High-performance async web framework |
| [uvicorn](https://github.com/encode/uvicorn) | >=0.35.0 | BSD-3-Clause | ASGI server for FastAPI |
| [Pydantic](https://github.com/pydantic/pydantic) | >=2.11.7 | MIT | Data validation and settings management |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | >=1.1.1 | BSD-3-Clause | Environment variable loading from .env files |
| [python-multipart](https://github.com/Kludex/python-multipart) | >=0.0.20 | Apache-2.0 | Multipart form data parser |
| [websockets](https://github.com/python-websockets/websockets) | >=15.0.1 | BSD-3-Clause | WebSocket client and server library |
| [requests](https://github.com/psf/requests) | >=2.28.0 | Apache-2.0 | HTTP library for Python |
| [asyncpg](https://github.com/MagicStack/asyncpg) | >=0.29.0 | Apache-2.0 | Async PostgreSQL client |

### LangChain & AI Orchestration

| Package | Version | License | Description |
|---------|---------|---------|-------------|
| [LangChain](https://github.com/langchain-ai/langchain) | >=0.3.27 | MIT | LLM application framework |
| [langchain-openai](https://github.com/langchain-ai/langchain) | >=0.3.28 | MIT | OpenAI integration for LangChain |
| [langchain-nvidia-ai-endpoints](https://github.com/langchain-ai/langchain-nvidia) | >=0.3.13 | MIT | NVIDIA AI Endpoints integration |
| [langchain-milvus](https://github.com/langchain-ai/langchain) | >=0.2.1 | MIT | Milvus vector store integration |
| [langchain-mcp-adapters](https://github.com/langchain-ai/langchain-mcp-adapters) | >=0.1.0 | MIT | Model Context Protocol adapters |
| [langchain-text-splitters](https://github.com/langchain-ai/langchain) | >=0.3.9 | MIT | Text chunking utilities |
| [langchain-unstructured](https://github.com/langchain-ai/langchain) | >=0.1.6 | MIT | Unstructured document loader |
| [LangGraph](https://github.com/langchain-ai/langgraph) | >=0.6.0 | MIT | Stateful multi-agent orchestration |
| [MCP](https://github.com/modelcontextprotocol/python-sdk) | >=0.1.0 | MIT | Model Context Protocol SDK |

### Document Processing

| Package | Version | License | Description |
|---------|---------|---------|-------------|
| [Unstructured](https://github.com/Unstructured-IO/unstructured) | >=0.18.11 | Apache-2.0 | Document parsing and extraction (with PDF support) |
| [PyPDF2](https://github.com/py-pdf/pypdf) | >=3.0.1 | BSD-3-Clause | PDF file reader |

### Key Transitive Dependencies

These are notable libraries pulled in as transitive dependencies:

| Package | License | Description |
|---------|---------|-------------|
| [Starlette](https://github.com/encode/starlette) | BSD-3-Clause | ASGI framework (used by FastAPI) |
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause | Async HTTP client |
| [SQLAlchemy](https://github.com/sqlalchemy/sqlalchemy) | MIT | SQL toolkit and ORM |
| [OpenAI Python](https://github.com/openai/openai-python) | Apache-2.0 | OpenAI API client |
| [tiktoken](https://github.com/openai/tiktoken) | MIT | Token counting for OpenAI models |
| [PyMilvus](https://github.com/milvus-io/pymilvus) | Apache-2.0 | Milvus Python SDK |
| [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) | MIT | HTML/XML parser |
| [lxml](https://github.com/lxml/lxml) | BSD-3-Clause | XML/HTML processing library |
| [Pillow](https://github.com/python-pillow/Pillow) | MIT-CMU | Image processing library |
| [NumPy](https://github.com/numpy/numpy) | BSD-3-Clause | Numerical computing library |
| [pandas](https://github.com/pandas-dev/pandas) | BSD-3-Clause | Data analysis library |
| [PyTorch](https://github.com/pytorch/pytorch) | BSD-3-Clause | Deep learning framework |
| [Transformers](https://github.com/huggingface/transformers) | Apache-2.0 | Hugging Face model library |
| [NLTK](https://github.com/nltk/nltk) | Apache-2.0 | Natural language toolkit |
| [SciPy](https://github.com/scipy/scipy) | BSD-3-Clause | Scientific computing library |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) | MIT | ML inference runtime |
| [Jinja2](https://github.com/pallets/jinja) | BSD-3-Clause | Template engine |
| [certifi](https://github.com/certifi/python-certifi) | MPL-2.0 | Mozilla CA certificate bundle |
| [protobuf](https://github.com/protocolbuffers/protobuf) | BSD-3-Clause | Protocol Buffers serialization |
| [cryptography](https://github.com/pyca/cryptography) | Apache-2.0 / BSD-3-Clause | Cryptographic primitives |
| [aiohttp](https://github.com/aio-libs/aiohttp) | Apache-2.0 | Async HTTP client/server |

---

## Infrastructure & Container Images

### Docker Base Images

| Image | Version | License | Usage |
|-------|---------|---------|-------|
| [node](https://hub.docker.com/_/node) (Alpine) | 20-alpine | MIT (Node.js) | Frontend build stage |
| [nginx](https://hub.docker.com/_/nginx) (Alpine) | alpine | BSD-2-Clause | Frontend static file serving |
| [python](https://hub.docker.com/_/python) (slim) | 3.12-slim | PSF License | Backend runtime |
| [NVIDIA CUDA](https://catalog.ngc.nvidia.com/orgs/nvidia/containers/cuda) | 13.0.1 | NVIDIA EULA (proprietary) | llama.cpp build/runtime |
| [NVIDIA TensorRT-LLM](https://catalog.ngc.nvidia.com/orgs/nvidia/containers/tensorrt-llm) | spark-single-gpu-dev | NVIDIA EULA (proprietary) | LLM serving (optional) |

### Infrastructure Services

| Service | Image | License | Purpose |
|---------|-------|---------|---------|
| [PostgreSQL](https://www.postgresql.org/) | postgres:15-alpine | PostgreSQL License (permissive) | Conversation and source storage |
| [Milvus](https://github.com/milvus-io/milvus) | milvusdb/milvus:v2.5.15 | Apache-2.0 | Vector database for embeddings |
| [etcd](https://github.com/etcd-io/etcd) | quay.io/coreos/etcd:v3.5.5 | Apache-2.0 | Distributed key-value store (Milvus coordination) |
| [MinIO](https://github.com/minio/minio) | minio/minio:RELEASE.2023-03-20 | AGPL-3.0 | Object storage (Milvus backend) |

### Tools Built From Source

| Tool | License | Purpose |
|------|---------|---------|
| [llama.cpp](https://github.com/ggml-org/llama.cpp) | MIT | LLM inference engine (built in Dockerfile.llamacpp) |

---

## AI Models

These models are downloaded and served at runtime. They are not bundled in the repository.

| Model | Provider | License | Purpose |
|-------|----------|---------|---------|
| [gpt-oss-120b](https://huggingface.co/nvidia) | NVIDIA | NVIDIA Open Model License | Supervisor LLM |
| [Qwen3-Embedding-4B](https://huggingface.co/Qwen) | Alibaba Cloud (Qwen) | Apache-2.0 | Document embedding/vectorization |

---

## Build Tools & Package Managers

| Tool | License | Purpose |
|------|---------|---------|
| [npm](https://github.com/npm/cli) | Artistic-2.0 | Node.js package manager (frontend) |
| [uv](https://github.com/astral-sh/uv) | Apache-2.0 / MIT | Python package manager (backend) |
| [Docker](https://www.docker.com/) | Apache-2.0 | Container runtime |
| [Kustomize](https://kustomize.io/) | Apache-2.0 | Kubernetes manifest management |

---

## License Summary

The following license types are used across all dependencies:

| License | Type | Notable Packages |
|---------|------|-----------------|
| **MIT** | Permissive | React, FastAPI, LangChain, LangGraph, Vite, Tailwind CSS, llama.cpp |
| **Apache-2.0** | Permissive | This project, TypeScript, Milvus, etcd, Transformers, Unstructured |
| **BSD-2-Clause** | Permissive | nginx |
| **BSD-3-Clause** | Permissive | uvicorn, NumPy, pandas, PyTorch, SciPy |
| **PostgreSQL License** | Permissive | PostgreSQL |
| **PSF License** | Permissive | Python |
| **AGPL-3.0** | Copyleft | MinIO (used as a standalone service) |
| **MPL-2.0** | Weak copyleft | certifi |
| **NVIDIA EULA** | Proprietary | CUDA containers, TensorRT-LLM |
| **NVIDIA Open Model License** | Model-specific | gpt-oss-120b |

### AGPL-3.0 Note

MinIO is licensed under AGPL-3.0 and is used as an independent, unmodified network service (object storage backend for Milvus). It is not linked into or distributed with the application code.

---

*This document was generated to provide transparency about the open source components used in this project. For the most current dependency versions, refer to `assets/frontend/package.json` and `assets/backend/pyproject.toml`.*
