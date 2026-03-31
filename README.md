# clevis

Basic analytics for GitHub repos and organizations using the GitHub API.

## Self-host

### Prerequisites
- Docker + Docker Compose

### Setup
1. Copy `.env.example` to `.env`
2. Create a local persistent directory `data/` in the repo root
3. Start the stack:
   `docker compose -f deploy/docker-compose.yml up --build -d`

Open the UI (default: `http://localhost:3000`).

