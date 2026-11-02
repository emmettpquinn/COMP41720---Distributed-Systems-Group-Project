"""Starts the Booking service. Run with:  python main.py"""
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

import config
import db
from api import router


@asynccontextmanager
async def lifespan(app):
    db.init_schema()   # create the seats table if it doesn't exist yet
    yield


app = FastAPI(title="Booking", lifespan=lifespan)
app.include_router(router)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=config.PORT)  # port: see config.py (temporary)
