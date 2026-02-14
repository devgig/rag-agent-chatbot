# Frontend

React application providing the user interface for Spark Chat.

## Overview

The frontend provides a chat interface with:
- Document upload and RAG (Retrieval Augmented Generation)
- Real-time streaming responses via WebSocket with auto-reconnection
- Theme switching (light/dark mode)
- Sidebar configuration for data sources and chat history

## Key Components

- **QuerySection**: Main chat interface with WebSocket streaming, token batching, and auto-reconnect
- **Sidebar**: Configuration panel for document sources and chat history management
- **DocumentIngestion**: File upload interface for RAG document ingestion
- **WelcomeSection**: Landing page with RAG agent card
- **ThemeToggle**: Dark/light mode switcher with localStorage persistence

## Architecture

Built with React 19, Vite 6, TypeScript, and CSS modules. Communicates with the backend via REST API and WebSocket connections for real-time chat streaming.

### WebSocket Features
- **Auto-reconnect**: Exponential backoff reconnection (up to 5 attempts) on unexpected disconnections
- **Token batching**: Accumulates streaming tokens and flushes every 50ms to reduce re-renders
- **Connection status**: User-visible error messages when connection is lost or reconnecting
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
│   └── api.ts               # Backend URL resolution, WebSocket URL generation
├── types/
│   └── config.ts            # TypeScript interfaces
├── components/
│   ├── QuerySection.tsx     # Chat UI, WebSocket client, markdown rendering
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
