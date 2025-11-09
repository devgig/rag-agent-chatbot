# Frontend

Next.js React application providing the user interface for the chatbot demo.

## Overview

The frontend provides a chat interface with support for:
- Multi-model conversations
- Document upload and RAG (Retrieval Augmented Generation)
- Image processing capabilities
- Real-time streaming responses via WebSocket
- Theme switching (light/dark mode)
- Sidebar configuration for models and data sources

## Key Components

- **QuerySection**: Main chat interface with message display and input
- **Sidebar**: Configuration panel for models, sources, and chat history
- **DocumentIngestion**: File upload interface for RAG functionality
- **WelcomeSection**: Landing page with quick-start templates
- **ThemeToggle**: Dark/light mode switcher

## Architecture

Built with Next.js 14, TypeScript, and CSS modules. Communicates with the backend via REST API and WebSocket connections for real-time chat streaming.

## Local Development

### Prerequisites
- Node.js 20.x or higher
- npm or yarn package manager

### Setup

1. **Install dependencies**:
   ```bash
   cd assets/frontend
   npm install
   ```

2. **Configure environment variables** (optional):
   Create a `.env.local` file:
   ```bash
   NEXT_PUBLIC_API_URL=http://localhost:8000
   ```

3. **Start development server**:
   ```bash
   npm run dev
   ```

   The frontend will be available at [http://localhost:3000](http://localhost:3000)

### Available Scripts

- `npm run dev` - Start development server with Turbopack
- `npm run build` - Build production bundle
- `npm start` - Start production server
- `npm run lint` - Run ESLint

### Development Workflow

1. Make changes to components in `src/` directory
2. Hot reload will automatically refresh the browser
3. Backend should be running on port 8000 for full functionality

## Docker Troubleshooting

### Container Issues
- **Port conflicts**: Ensure port 3000 is not in use by other applications
- **Build failures**: Clear Docker cache with `docker system prune -a`
- **Hot reload not working**: Restart the container or check volume mounts

### Common Commands
```bash
# View frontend logs
docker logs frontend

# Restart frontend container
docker restart frontend

# Rebuild frontend
docker-compose up --build -d frontend

# Access container shell
docker exec -it frontend /bin/sh
```

### Performance Issues
- Check available memory: `docker stats`
- Increase Docker memory allocation in Docker Desktop settings
- Clear browser cache and cookies for localhost:3000
