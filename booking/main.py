"""Starts the Booking service. Run with:  python main.py"""
import uvicorn
from fastapi import FastAPI

import config
from api import router

app = FastAPI(title="Booking")
app.include_router(router)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=config.PORT)  # port: see config.py (temporary)
