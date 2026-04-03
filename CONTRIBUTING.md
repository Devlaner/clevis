# Contributing to clevis

Thanks for helping improve clevis. This document describes how to work in the repo so your changes match what CI expects.

## Project layout

| Path | Role |
|------|------|
| `apps/ui` | Next.js UI (Node 22, npm) |
| `apps/api` | Python API |
| `apps/worker` | Python worker |
| `deploy/` | Docker Compose and deployment assets |

## Prerequisites

- **Node.js 22** (same as CI)
- **Python 3.12** (for API/worker checks)
- **Docker** (for image build verification)

## Initial setup

1. Clone the repository.
2. From the **repository root**, install tooling and enable Git hooks:

   ```bash
   npm ci
   ```

   The `prepare` script registers Husky. After this, commits run local checks (see below).

3. Install UI dependencies when working on the frontend:

   ```bash
   cd apps/ui
   npm ci
   ```

## Line endings

The repo uses `.gitattributes` so text files are stored with **LF**. On Windows, let Git handle normalization (`core.autocrlf` is usually fine with `true` or `input`). Shell hook scripts (`.husky/*`) should stay LF.

## Git hooks (Husky)

After `npm ci` at the repo root:

- **`pre-commit`** runs `npm --prefix apps/ui run typecheck`. Ensure `apps/ui` dependencies are installed so this succeeds.
- **`commit-msg`** runs [Commitlint](https://commitlint.js.org/) with the [Conventional Commits](https://www.conventionalcommits.org/) preset (`@commitlint/config-conventional`).

To bypass hooks in exceptional cases (not recommended for routine work): `git commit --no-verify`.

## Commit messages

Use **Conventional Commits**, for example:

- `feat(ui): add repo filter to sidebar`
- `fix(api): handle rate-limit responses`
- `chore: bump worker deps`

CI runs Commitlint on the commits in each push or pull request, so messages that do not follow the convention will fail the **Commit Messages** check.

## Checks to run before opening a PR

These mirror [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

### UI

```bash
cd apps/ui
npm ci
npm run typecheck
npm run build
```

(`npm run check` runs typecheck and build together.)

### Python (API + worker)

```bash
python -m pip install --upgrade pip
python -m pip install -r apps/api/requirements.txt
python -m pip install -r apps/worker/requirements.txt
python -m compileall apps/api/src apps/worker/src
```

### Docker images

From the **repository root**:

```bash
docker build -t clevis-api -f apps/api/Dockerfile .
docker build -t clevis-worker -f apps/worker/Dockerfile apps/worker
docker build -t clevis-ui -f apps/ui/Dockerfile apps/ui
```

## Pull requests

- Open a PR against the branch your maintainers use as the integration target (often `main`).
- Keep changes focused; unrelated drive-by refactors make review harder.
- Ensure all CI jobs pass (commits, UI, Python, Docker builds).

## Security and configuration

- Do not commit secrets. Use `.env` locally (see `.env.example` and the main README for self-host setup).
- If you change ignore rules, remember that a bare `lib/` entry in `.gitignore` ignores **every** `lib` directory in the tree; root-only patterns like `/lib/` avoid accidentally excluding app code (for example under `apps/ui/lib`).

## Questions

If something in this doc is outdated or unclear, opening an issue or a small doc-fix PR is welcome.
