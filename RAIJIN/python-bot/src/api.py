from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from loguru import logger
from src.config import settings
import sys

# Configure loguru
logger.remove()
logger.add(sys.stdout, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {message}", level="INFO")
logger.add("logs/raijin.log", rotation="10 MB", retention="30 days", level="DEBUG")

app = FastAPI(title="RAIJIN Bot", version="0.1.0")


class Signal(BaseModel):
    id: str
    receivedAt: int
    action: str
    ticker: str
    strategy: Optional[str] = None
    timeframe: Optional[str] = None
    price: Optional[float] = None
    atr: Optional[float] = None
    rsi: Optional[float] = None
    regime: Optional[str] = None
    ivr: Optional[float] = None
    volRatio: Optional[float] = Field(None, alias="volRatio")
    source: Optional[str] = None


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "env": settings.ENV,
        "version": "0.1.0",
    }


@app.post("/signal")
async def receive_signal(signal: Signal, request: Request):
    # Validate internal secret
    secret = request.headers.get("X-Internal-Secret", "")
    if secret != settings.INTERNAL_SECRET:
        logger.warning(f"Unauthorized signal attempt from {request.client.host}")
        raise HTTPException(status_code=401, detail="Unauthorized")

    logger.info(f"SIGNAL RECEIVED | {signal.action.upper()} {signal.ticker} @ {signal.price} | strategy={signal.strategy} | regime={signal.regime}")
    logger.debug(f"Full signal payload: {signal.model_dump()}")

    # Paper mode guard — no orders fire
    if settings.ENV == "paper":
        logger.info(f"ENV=paper — signal logged, no orders fired")
        return {
            "status": "received",
            "env": settings.ENV,
            "signal_id": signal.id,
            "action": signal.action,
            "ticker": signal.ticker,
            "orders_fired": False,
        }

    # Future: live signal processing pipeline goes here
    # 1. IV regime check
    # 2. GEX regime check
    # 3. Strategy selection
    # 4. Risk engine validation
    # 5. Order construction
    # 6. Execution via IBKR

    return {
        "status": "received",
        "env": settings.ENV,
        "signal_id": signal.id,
        "action": signal.action,
        "ticker": signal.ticker,
        "orders_fired": False,
    }


@app.post("/halt")
async def halt(request: Request):
    secret = request.headers.get("X-Internal-Secret", "")
    if secret != settings.INTERNAL_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    logger.warning("HALT COMMAND RECEIVED — killing all activity")
    # Future: cancel all open orders, close positions
    return {"status": "halted", "env": settings.ENV}
