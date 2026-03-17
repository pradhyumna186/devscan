# DevScan

AI-powered code review tool that analyzes GitHub repositories and surfaces issues by severity via a GraphQL API.

## TODO — 6-Day Build Plan

- [ ] **Day 1** — Project scaffold, Docker setup, DB models, Alembic migrations
- [ ] **Day 2** — GraphQL schema + resolvers (queries & mutations), DB wired up
- [ ] **Day 3** — Anthropic LLM integration: `analyze_code` service, prompt engineering, issue parsing
- [ ] **Day 4** — Redis caching layer: hash-based cache for repeated repo/code submissions
- [ ] **Day 5** — Background task queue (Celery or asyncio): async review processing, status polling
- [ ] **Day 6** — Polish: error handling, pagination, input validation, integration tests, README finalize

## Quick Start

```bash
cp .env.example .env
# Fill in ANTHROPIC_API_KEY in .env
docker-compose up -d
# App available at http://localhost:8000/graphql
```

## Stack

- **FastAPI** + **Strawberry GraphQL**
- **PostgreSQL** (via SQLAlchemy async + asyncpg)
- **Redis** (caching by code hash)
- **Anthropic Claude** (code analysis)
- **Alembic** (migrations)
