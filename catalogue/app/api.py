"""Catalogue HTTP routes. Availability is informative, never hold authorization."""

import asyncio

import asyncpg
from fastapi import APIRouter, HTTPException, Request

from . import repository
from .schemas import EventCreate, EventPatch, VenueCreate

router = APIRouter()


async def database_call(request, operation):
    try:
        async with asyncio.timeout(request.app.state.settings.database_timeout_seconds):
            async with request.app.state.database_pool.acquire() as connection:
                return await operation(connection)
    except repository.NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except repository.Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(409, "Identifier already exists") from exc
    except asyncpg.ForeignKeyViolationError as exc:
        raise HTTPException(409, "Referenced data changed; reload and retry") from exc
    except (asyncpg.PostgresError, OSError, TimeoutError) as exc:
        raise HTTPException(503, "Catalogue database is unavailable") from exc


@router.post("/venues", status_code=201)
async def create_venue(data: VenueCreate, request: Request):
    return await database_call(request, lambda connection: repository.create_venue(connection, data))


@router.get("/venues/{venue_id}")
async def get_venue(venue_id: str, request: Request):
    return await database_call(request, lambda connection: repository.get_venue(connection, venue_id))


@router.get("/events")
async def list_events(request: Request):
    return await database_call(request, repository.list_events)


@router.get("/events/{event_id}")
async def get_event(event_id: str, request: Request):
    cache = request.app.state.cache
    cached = cache.get(event_id)
    request.app.state.metrics.cache.labels("hit" if cached is not None else "miss").inc()
    if cached is not None:
        return cached
    generation = cache.generation
    result = await database_call(request, lambda connection: repository.event_detail(connection, event_id))
    cache.put(event_id, result, generation)
    return result


@router.post("/events", status_code=201)
async def create_event(data: EventCreate, request: Request):
    result = await database_call(request, lambda connection: repository.create_event(connection, data))
    request.app.state.cache.invalidate(data.event_id)
    return result


@router.patch("/events/{event_id}")
async def update_event(event_id: str, data: EventPatch, request: Request):
    result = await database_call(request, lambda connection: repository.update_event(connection, event_id, data))
    request.app.state.cache.invalidate(event_id)
    return result
