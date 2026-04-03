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

## CI / CD
CI runs on **every pull request** and on **pushes to any branch** and verifies:
- UI TypeScript typecheck + production UI build
- Python source compilation
- Docker image build for `api`, `worker`, and `ui`

On version tags (`v*`), Docker images are published to GHCR so others can self-host without rebuilding from source:
- `ghcr.io/<owner>/<repo>-api`
- `ghcr.io/<owner>/<repo>-worker`
- `ghcr.io/<owner>/<repo>-ui`

