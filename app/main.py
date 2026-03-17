from contextlib import asynccontextmanager
from typing import AsyncGenerator

import strawberry
from fastapi import FastAPI
from strawberry.fastapi import GraphQLRouter

from app.database import get_session, init_db
from app.resolvers.mutations import Mutation
from app.resolvers.queries import Query


# ──────────────────────────────────────────
# GraphQL schema
# ──────────────────────────────────────────

schema = strawberry.Schema(query=Query, mutation=Mutation)


# ──────────────────────────────────────────
# Context: inject DB session per request
# ──────────────────────────────────────────

async def get_context(db=get_session) -> dict:
    """Strawberry context factory — provides the async DB session."""
    async for session in get_session():
        return {"db": session}


# ──────────────────────────────────────────
# Lifespan: DB initialisation
# ──────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await init_db()
    yield
    # Graceful teardown (add Redis close here later)
    from app.services.cache import close as close_redis
    await close_redis()


# ──────────────────────────────────────────
# App factory
# ──────────────────────────────────────────

app = FastAPI(
    title="DevScan",
    description="AI-powered code review via GraphQL",
    version="0.1.0",
    lifespan=lifespan,
)

graphql_app = GraphQLRouter(schema, context_getter=get_context)
app.include_router(graphql_app, prefix="/graphql")


@app.get("/health", tags=["meta"])
async def health_check():
    return {"status": "ok"}
