# Frontend

React application providing the user interface for Spark Chat.

## Overview

The frontend provides a chat interface with:
- Document upload and RAG (Retrieval Augmented Generation)
- Real-time streaming responses via SSE (Server-Sent Events)
- Theme switching (light/dark mode)
- Sidebar configuration for data sources and chat history

## Key Components

- **QuerySection**: Main chat interface with SSE streaming with abort support and token batching
- **Sidebar**: Configuration panel with single-select context source (radio buttons), private/public badges, and chat history management
- **DocumentIngestion**: File upload interface for RAG document ingestion
- **WelcomeSection**: Landing page with RAG agent card
- **ThemeToggle**: Dark/light mode switcher with localStorage persistence

## Architecture

Built with React 19, Vite 6, TypeScript, and CSS modules. Communicates with the backend via REST API and SSE (Server-Sent Events) for real-time chat streaming.

### Streaming Features
- **SSE-based**: Each query is a POST that returns a text/event-stream; no persistent connection needed
- **Token batching**: Accumulates streaming tokens with requestAnimationFrame-based throttle
- **Cancellation**: AbortController support; partial responses saved to Redis
- **Istio session affinity**: Chat ID passed as query parameter for consistent hashing

## Local Development

### Prerequisites
- Node.js 20.x or higher
- npm package manager

### Setup

1. **Install dependencies**:
   ```bash
   cd assets/frontend
   npm install
   ```

2. **Start development server**:
   ```bash
   npm run dev
   ```

   The frontend will be available at [http://localhost:3000](http://localhost:3000)

### Available Scripts

- `npm run dev` - Start development server on port 3000
- `npm run build` - Build production bundle
- `npm run preview` - Preview production build
- `npm run lint` - Run ESLint

### Development Workflow

1. Make changes to components in `src/` directory
2. Vite hot module replacement automatically refreshes the browser
3. Backend should be running on port 8000 for full functionality

## Project Structure

```
src/
├── main.tsx                 # Entry point
├── App.tsx                  # Root component, global state
├── index.css                # CSS variables, theme definitions
├── lib/
│   └── api.ts               # Backend URL resolution, SSE stream helpers
├── types/
│   └── config.ts            # TypeScript interfaces
├── components/
│   ├── QuerySection.tsx     # Chat UI, SSE client, markdown rendering
│   ├── Sidebar.tsx          # Source/chat management, collapsible sections
│   ├── WelcomeSection.tsx   # Landing page
│   ├── DocumentIngestion.tsx # File upload with drag-and-drop
│   └── ThemeToggle.tsx      # Dark/light mode toggle
└── styles/
    ├── QuerySection.module.css
    ├── Sidebar.module.css
    ├── WelcomeSection.module.css
    ├── DocumentIngestion.module.css
    └── Home.module.css
```

## Docker Troubleshooting

### Common Commands
```bash
docker logs frontend        # View logs
docker restart frontend     # Restart container
docker exec -it frontend sh # Access shell
```
