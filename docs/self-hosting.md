# Self-hosting

## Requirements
- Docker + Docker Compose
- GitHub App credentials (or PAT for testing)

## Setup
1. Copy `.env.example` to `.env` and update values.
2. Create local persistent directory `data/` in repo root.
3. Start stack:
   - `docker compose -f deploy/docker-compose.yml up --build -d`

## Security notes
- Use GitHub App auth in production, not PAT.
- Restrict API ingress behind your reverse proxy and SSO.
- Set `X-Role: admin` only for trusted automation/users.

## Observability
- API has request-id logs and health endpoint at `/healthz`.
- Worker logs completed/failed jobs for cache deletion.
