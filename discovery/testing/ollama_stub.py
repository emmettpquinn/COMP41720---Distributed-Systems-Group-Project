"""Ollama-compatible HTTP simulator with failure controls for repeatable tests.

No learned model runs here. Deterministic vectors and token counts are synthetic.
Keep this on a local/test network; the control route intentionally has no auth.
"""
import asyncio
import hashlib
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from discovery.app.simulator import SimulatedOllama

app = FastAPI(title='SIMULATED Ollama protocol for integration tests')
simulator = SimulatedOllama()


class Faults(BaseModel):
    delay_seconds: float = Field(default=0, ge=0, le=30)
    fail: bool = False


faults = Faults()
active_calls = 0
peak_calls = 0


class Request(BaseModel):
    model: str
    input: str


def digest(model):
    return hashlib.sha256(('SIMULATED:' + model).encode()).hexdigest()


@app.get('/api/tags')
async def tags():
    return {'models': [{'name': name, 'model': name, 'digest': digest(name)}
                       for name in ('nomic-embed-text:latest', 'test-model:v2')]}


@app.post('/api/embed')
async def embed(request: Request):
    global active_calls, peak_calls
    if request.model not in {'nomic-embed-text:latest', 'test-model:v2'}:
        raise HTTPException(404, 'Unknown simulated model')
    active_calls += 1
    peak_calls = max(peak_calls, active_calls)
    try:
        delay, fail = faults.delay_seconds, faults.fail
        if delay:
            await asyncio.sleep(delay)
        if fail:
            raise HTTPException(503, 'Injected simulated model outage')
        size = 768 if request.model == 'nomic-embed-text:latest' else 64
        vector = await simulator.embed(request.input, request.model, size)
        return {'model': request.model, 'embeddings': [vector],
                'prompt_eval_count': len(request.input.split()), 'simulated': True}
    finally:
        active_calls -= 1


@app.post('/test/faults')
async def configure(body: Faults):
    global faults, peak_calls
    faults = body
    peak_calls = active_calls
    return {'simulated': True, **faults.model_dump()}


@app.get('/test/status')
async def status():
    return {'simulated': True, 'active_calls': active_calls, 'peak_calls': peak_calls}
