"""Starts the Booking service. Run with:  python main.py"""
from contextlib import asynccontextmanager

import os

import uvicorn
from fastapi import FastAPI

import config
import db
from api import router


@asynccontextmanager
async def lifespan(app):
    db.init_schema()
    if os.getenv("BOOKING_DEMO_SEED", "false").lower() == "true":
        from demo_seed import seed
        seed()
    yield


app = FastAPI(title="Booking", lifespan=lifespan)
app.include_router(router)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=config.PORT)  # port: see config.py (temporary)
