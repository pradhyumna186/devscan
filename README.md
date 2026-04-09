# DevScan — AI GitHub PR Review Bot

DevScan is a self-hosted GitHub PR review bot that automatically detects bugs, security vulnerabilities, and code quality issues in pull requests. When a PR is opened or updated, DevScan fetches the diff, runs a deterministic secret scanner and an LLM-powered code analysis, then posts inline review comments directly on the PR — all without sending your code to a third-party AI service.

---

## Features

- **Automatic PR analysis** — triggered by GitHub webhooks on `opened` and `synchronize` events
- **Secret & credential scanner** — deterministic regex engine catches committed `.env` files, API keys, AWS credentials, GitHub tokens, private keys, and database connection strings before the LLM even runs
- **LLM code review** — per-file parallel analysis using any OpenAI-compatible local model (Ollama, LM Studio, vLLM, etc.)
- **Prior-issue context** — re-reviews pass existing findings to the LLM so it focuses on new issues rather than duplicating old ones
- **Inline GitHub comments** — issues posted as inline diff comments with severity badges and suggested fixes
- **Redis caching** — identical diffs skip redundant LLM calls (24-hour TTL); `forceReReview` bypasses the cache
- **GraphQL API** — query reviews, filter by repo/status/severity, trigger manual reviews, and subscribe to real-time status updates
- **Real-time subscriptions** — WebSocket-based GraphQL subscriptions stream `PENDING → ANALYZING → POSTED | FAILED` status transitions live
- **AWS ECS deploy** — includes a Fargate task definition with Secrets Manager integration for production deployment

---

## Architecture

```
GitHub Webhook (PR open/update)
        │
        ▼
┌───────────────────┐
│   FastAPI app     │  POST /webhook/github
│                   │  *    /graphql (Strawberry)
└────────┬──────────┘
         │  Background task
         ▼
┌───────────────────────────────────────┐
│           Analysis Pipeline           │
│                                       │
│  1. Fetch PR diff  (GitHub API)       │
│  2. Load prior issues  (Postgres)     │
│  3. Secret scanner  (regex, sync)     │
│  4. Cache check  (Redis)              │
│  5. LLM analysis  (per-file, async)   │
│  6. Merge & deduplicate findings      │
│  7. Persist issues  (Postgres)        │
│  8. Post inline comments  (GitHub)    │
└───────────────────────────────────────┘
         │
         ▼
   GraphQL Subscriptions (Redis Pub/Sub)
```

**Stack:** Python 3.12 · FastAPI · Strawberry GraphQL · SQLAlchemy (async) · PostgreSQL · Redis · httpx · Alembic · Docker

---

## Project Structure

```
app/
├── main.py                   # FastAPI app, webhook endpoint, lifespan
├── models.py                 # SQLAlchemy models (Repo, PRReview, Issue, WebhookEvent)
├── schema.py                 # Strawberry GraphQL types and input types
├── database.py               # Async engine, session factory
├── resolvers/
│   ├── queries.py            # prReviews, prReview, issuesBySeverity
│   ├── mutations.py          # triggerReview, forceReReview, deleteReview
│   └── subscriptions.py      # reviewStatusUpdated (WebSocket)
└── services/
    ├── pipeline.py           # Orchestrates the full analysis flow
    ├── secret_scanner.py     # Deterministic regex-based credential scanner
    ├── llm.py                # Per-file parallel LLM analysis
    ├── github.py             # Fetch PR diffs, post inline review comments
    ├── cache.py              # Redis diff-hash caching
    └── pubsub.py             # Redis Pub/Sub for real-time status updates

migrations/                   # Alembic migration scripts
deploy/
└── task-definition.json      # AWS ECS Fargate task definition
```

---

## Getting Started

### Prerequisites

- Docker and Docker Compose
- A GitHub Personal Access Token with `repo` and `pull_requests` scopes
- An Ollama server (or any OpenAI-compatible LLM endpoint)
- A public URL for your server (use [ngrok](https://ngrok.com) for local development)

### 1. Clone and configure

```bash
git clone https://github.com/pradhyumna186/DevScan.git
cd DevScan
cp .env.example .env
```

Edit `.env`:

```env
DATABASE_URL=postgresql+asyncpg://devscan:devscan_pass@db:5432/devscan
REDIS_URL=redis://redis:6379

# Point to your Ollama / LM Studio / vLLM server
# Use host.docker.internal when running inside Docker on Mac/Windows
REMOTE_LLM_BASE_URL=http://host.docker.internal:11434
REMOTE_LLM_MODEL=qwen2.5-coder:7b
REMOTE_LLM_API_KEY=          # leave blank for Ollama
REMOTE_LLM_TIMEOUT=120

GITHUB_WEBHOOK_SECRET=your_webhook_secret
GITHUB_APP_TOKEN=your_github_pat
```

### 2. Start the stack

```bash
docker compose up -d
```

This starts:
- `devscan_app` on port `8000`
- `devscan_postgres` on port `5433`
- `devscan_redis` on port `6379`

### 3. Run database migrations

```bash
docker compose exec app alembic upgrade head
```

### 4. Configure the GitHub webhook

In your GitHub repository → **Settings → Webhooks → Add webhook**:

| Field | Value |
|---|---|
| Payload URL | `http://<your-server>:8000/webhook/github` |
| Content type | `application/json` |
| Secret | The value of `GITHUB_WEBHOOK_SECRET` in your `.env` |
| Events | Select **Pull requests** |

For local development, expose port 8000 with ngrok:
```bash
ngrok http 8000
```

---

## GraphQL API

Open the interactive playground at `http://localhost:8000/graphql`.

### Queries

```graphql
# List recent reviews (filterable by repo and status)
query {
  prReviews(repoName: "owner/repo", status: "POSTED", limit: 10) {
    id
    prNumber
    prTitle
    status
    createdAt
    issues {
      severity
      filePath
      lineNumber
      description
      suggestedFix
    }
  }
}

# Fetch a single review by ID
query {
  prReview(id: "12") {
    status
    issues { severity filePath description }
  }
}

# All critical issues across a repo
query {
  issuesBySeverity(severity: "CRITICAL", repoName: "owner/repo") {
    filePath
    lineNumber
    description
  }
}
```

### Mutations

```graphql
# Manually trigger a review for any PR
mutation {
  triggerReview(repoFullName: "owner/repo", prNumber: 42) {
    id
    status
  }
}

# Re-run analysis, bypassing the cache (useful after FAILED reviews)
mutation {
  forceReReview(reviewId: "12") {
    id
    status
  }
}

# Delete a review and all its issues
mutation {
  deleteReview(reviewId: "12")
}
```

### Subscriptions

```graphql
# Stream real-time status updates (via WebSocket)
subscription {
  reviewStatusUpdated(reviewId: "12") {
    id
    status
    issues {
      severity
      filePath
      description
    }
  }
}
```

The stream emits the current state immediately on connect, then one update per transition (`PENDING → ANALYZING → POSTED | FAILED`), then closes automatically.

---

## How the Analysis Works

### Stage 1 — Secret Scanner (always runs)

Before calling the LLM, a deterministic regex engine scans every added line in the diff for:

- Committed `.env`, `.pem`, `.key`, `.p12` files
- Generic `API_KEY=`, `SECRET_KEY=`, `PASSWORD=`, `TOKEN=` assignments
- AWS access key IDs and secret keys
- GitHub PATs (classic `ghp_`, fine-grained `github_pat_`, app tokens `ghs_`)
- PEM private key headers
- Database connection strings with embedded credentials

These findings are injected directly into the results — no LLM required.

### Stage 2 — LLM Analysis (per-file, parallel)

The diff is split into per-file chunks. Each chunk is analyzed by the LLM in parallel (`asyncio.gather`), so the model focuses on one file at a time rather than processing a large monolithic diff. The prompt explicitly instructs the model to:

1. Trace the logic of every modified function (check operators, return values, edge cases)
2. Look for security vulnerabilities
3. Identify bugs, resource leaks, and performance issues

If previous reviews exist for the same PR, their findings are passed as context so the model doesn't re-report known issues.

### Stage 3 — Merge & Post

Secret scanner results and LLM results are merged and deduplicated, then posted as inline GitHub review comments with `[CRITICAL]` / `[MAJOR]` / `[MINOR]` severity badges and suggested fixes.

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | Yes | — | Async PostgreSQL connection string |
| `REDIS_URL` | Yes | `redis://localhost:6379` | Redis connection URL |
| `REMOTE_LLM_BASE_URL` | Yes | — | Base URL of OpenAI-compatible LLM server |
| `REMOTE_LLM_MODEL` | No | `qwen2.5-coder:7b` | Model name to use |
| `REMOTE_LLM_API_KEY` | No | _(blank)_ | Bearer token for authenticated endpoints |
| `REMOTE_LLM_TIMEOUT` | No | `120` | Per-request LLM timeout in seconds |
| `LLM_MAX_CHUNK_LINES` | No | `300` | Max diff lines per LLM call before splitting |
| `GITHUB_APP_TOKEN` | Yes | — | GitHub PAT with `repo` + `pull_requests` scope |
| `GITHUB_WEBHOOK_SECRET` | No | _(blank)_ | HMAC-SHA256 secret for webhook verification |

---

## Deployment (AWS ECS Fargate)

A ready-to-use task definition is provided in `deploy/task-definition.json`. It pulls secrets from AWS Secrets Manager and uses CloudWatch Logs.

```bash
# Build and push to ECR
aws ecr get-login-password | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
docker build -t devscan .
docker tag devscan:latest <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/devscan:latest
docker push <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/devscan:latest

# Register the task definition (after filling in ACCOUNT_ID placeholders)
aws ecs register-task-definition --cli-input-json file://deploy/task-definition.json
```

Required Secrets Manager secrets:
- `devscan/DATABASE_URL`
- `devscan/REDIS_URL`
- `devscan/GITHUB_APP_TOKEN`
- `devscan/GITHUB_WEBHOOK_SECRET`

---

## Data Model

```
Repo ──< PRReview ──< Issue
              └──< WebhookEvent (raw event log)

PRReview.status: PENDING → ANALYZING → POSTED | FAILED
Issue.severity:  CRITICAL | MAJOR | MINOR
```
