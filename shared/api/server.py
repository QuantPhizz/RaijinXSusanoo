# =============================================================================
# TKO-AGENTS — Shared API Server
# Version: 0.3.0-phase0
# Updated: June 4 2026 — PDT rule eliminated (FINRA Rule 4210 amended)
#   - /pdt endpoint replaced with /intraday
#   - slot arbitration logic removed
#   - intraday_trades table replaces pdt_counter
#   - /status reflects intraday framework, not slot count
# Target: /Users/shugogeta/tko-agents/RaijinXSusanoo/shared/api/server.py
# =============================================================================

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
API_KEY        = os.getenv("API_KEY", "")
IBKR_ENV       = os.getenv("IBKR_ENV", "paper")   # NEVER default to live
DATABASE_URL   = os.getenv("DATABASE_URL", "")
# Legacy split DB_* vars — fallback only; DATABASE_URL takes precedence
DB_HOST        = os.getenv("DB_HOST", "localhost")
DB_PORT        = int(os.getenv("DB_PORT", "5432"))
DB_NAME        = os.getenv("DB_NAME", "tko_agents")
DB_USER        = os.getenv("DB_USER", "")
DB_PASSWORD    = os.getenv("DB_PASSWORD", "")
CORS_ORIGINS   = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]

_START_TIME = time.time()

# ---------------------------------------------------------------------------
# Signal deduplication
#
# Deterministic hash over the canonical event-defining fields. If TradingView
# retries a webhook (5xx or timeout on our end), the retry produces the same
# key, and the UNIQUE constraint on signals.signal_dedup_key causes the second
# INSERT to conflict — we then return the original signal_id, making TV's
# retry idempotent.
#
# Formula MUST match the backfill SQL in migration 003 exactly:
#     sha256(system || '|' || ticker || '|' || strategy || '|' || direction || '|' || timestamp)
# with normalization:
#     - direction upper-cased (Pine emits 'buy', schema expects 'BUY')
#     - strategy defaulted to '' if missing
#     - timestamp emitted as ISO8601 string (Pydantic-parsed datetime.isoformat())
# ---------------------------------------------------------------------------

def compute_dedup_key(
    system: str,
    ticker: str,
    strategy: str | None,
    direction: str,
    timestamp: datetime,
) -> str:
    """
    Return the sha256 hex digest that uniquely identifies this signal event.

    Any change to this formula MUST be paired with a data migration to
    recompute existing dedup keys — otherwise old rows and new rows will
    have mutually inconsistent keys.
    """
    canonical = "|".join([
        (system or "").upper(),
        (ticker or "").upper(),
        (strategy or ""),
        (direction or "").upper(),
        timestamp.isoformat(),
    ])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("tko-agents")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="TKO-AGENTS Gateway",
    version="0.3.0-phase0",
    description=(
        "Shared signal gateway for RAIJIN and SUSANOO. "
        "Phase 0: logging scaffold only. "
        "PDT rule eliminated June 4 2026 — intraday margin framework applies."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Lifecycle: DB connection pool
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    # DATABASE_URL takes precedence; fall back to split DB_* vars
    if DATABASE_URL:
        dsn = DATABASE_URL
    elif DB_USER:
        dsn = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    else:
        dsn = f"postgresql://{DB_HOST}:{DB_PORT}/{DB_NAME}"

    app.state.db = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5)
    db_host_display = DATABASE_URL.split("@")[-1] if DATABASE_URL else f"{DB_HOST}:{DB_PORT}/{DB_NAME}"
    logger.info(f"DB pool connected → {db_host_display}")
    logger.info(f"IBKR env: {IBKR_ENV}")
    logger.info("PDT rule eliminated June 4 2026 — intraday margin framework in effect")
    if IBKR_ENV == "live":
        logger.warning("⚠️  IBKR_ENV=live — real money is at stake.")


@app.on_event("shutdown")
async def shutdown():
    await app.state.db.close()
    logger.info("DB pool closed.")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class WebhookPayload(BaseModel):
    system:    str
    secret:    str
    ticker:    str
    direction: str
    price:     float
    atr:       float
    rsi:       float
    regime:    str
    timestamp: datetime
    # Optional Phase 1+ fields — accepted but ignored in Phase 0
    ivr:       float | None = None
    vix:       float | None = None
    intraday:  bool | None = False   # SUSANOO intraday gate (Phase S1)
    strategy:  str | None = None     # optional — used in dedup key, Patch 003

    @field_validator("system")
    @classmethod
    def system_must_be_valid(cls, v):
        if v.upper() not in ("RAIJIN", "SUSANOO"):
            raise ValueError("system must be RAIJIN or SUSANOO")
        return v.upper()

    @field_validator("direction")
    @classmethod
    def direction_must_be_valid(cls, v):
        if v.upper() not in ("BUY", "SELL"):
            raise ValueError("direction must be BUY or SELL")
        return v.upper()


class SignalResponse(BaseModel):
    signal_id:     int
    system:        str
    status:        str
    was_duplicate: bool
    message:       str


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def validate_webhook_secret(secret: str):
    if not WEBHOOK_SECRET:
        raise HTTPException(500, "WEBHOOK_SECRET not configured on server")
    if secret != WEBHOOK_SECRET:
        raise HTTPException(401, "Invalid webhook secret")


def validate_api_key(x_api_key: str):
    if not API_KEY:
        raise HTTPException(500, "API_KEY not configured on server")
    if x_api_key != API_KEY:
        raise HTTPException(401, "Invalid API key")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def db_insert_signal(pool: asyncpg.Pool, payload: WebhookPayload) -> tuple[int, bool]:
    """
    Insert signal row. Status starts as ACCEPTED (Phase 0 bypass).

    Idempotent on signal_dedup_key: if TradingView retries a webhook and the
    retry collides with an existing dedup key, ON CONFLICT DO NOTHING no-ops
    the insert and we look up + return the original signal_id instead.

    Returns (signal_id, was_duplicate).
    """
    raw = payload.model_dump()
    raw["timestamp"] = raw["timestamp"].isoformat()

    dedup_key = compute_dedup_key(
        system=payload.system,
        ticker=payload.ticker,
        strategy=payload.strategy,
        direction=payload.direction,
        timestamp=payload.timestamp,
    )

    # Provenance tags for E2 fidelity guard. Phase 0: signals originate
    # directly from TradingView Pine, no gate evaluation has run — source
    # reflects origin, fidelity is UNKNOWN because no data provider was
    # consulted for this signal.
    signal_source   = "TRADINGVIEW_PINE"
    signal_fidelity = "UNKNOWN"

    row = await pool.fetchrow(
        """
        INSERT INTO signals (
            system, ticker, direction,
            tv_price, tv_atr, tv_rsi, tv_regime,
            raw_payload, status,
            decision_price, decision_ts,
            source, fidelity, signal_dedup_key
        ) VALUES (
            $1, $2, $3,
            $4, $5, $6, $7,
            $8, 'ACCEPTED',
            $4, NOW(),
            $9, $10, $11
        )
        ON CONFLICT (signal_dedup_key) DO NOTHING
        RETURNING id
        """,
        payload.system,
        payload.ticker,
        payload.direction,
        payload.price,
        payload.atr,
        payload.rsi,
        payload.regime,
        json.dumps(raw),
        signal_source,
        signal_fidelity,
        dedup_key,
    )

    if row is not None:
        return row["id"], False

    # ON CONFLICT hit — fetch the existing row's id so TV's retry sees success
    row = await pool.fetchrow(
        "SELECT id FROM signals WHERE signal_dedup_key = $1", dedup_key
    )
    logger.info(f"[dedup] retry detected, returning existing signal_id={row['id']}")
    return row["id"], True


async def db_is_frozen(pool: asyncpg.Pool, system: str) -> bool:
    row = await pool.fetchrow(
        "SELECT is_frozen FROM capital_fence WHERE system = $1", system
    )
    return bool(row["is_frozen"]) if row else False


async def db_count_open_positions(pool: asyncpg.Pool) -> int:
    row = await pool.fetchrow("SELECT COUNT(*) AS n FROM v_open_positions")
    return int(row["n"]) if row else 0


async def db_intraday_summary(pool: asyncpg.Pool) -> dict:
    """
    Intraday trade count for the current session.
    Replaces PDT slot logic — there is no slot limit.
    The binding constraint is now IBKR's real-time intraday margin engine.
    """
    row = await pool.fetchrow("SELECT * FROM v_intraday_summary")
    if row:
        return dict(row)
    return {
        "intraday_trades_today": 0,
        "raijin_intraday": 0,
        "susanoo_intraday": 0,
        "trading_date": datetime.now(timezone.utc).date().isoformat(),
    }


# ---------------------------------------------------------------------------
# Core signal processor (Phase 0 stub)
# ---------------------------------------------------------------------------

async def process_signal(pool: asyncpg.Pool, payload: WebhookPayload) -> SignalResponse:
    """
    Phase 0: log signal to PostgreSQL, return ACCEPTED.
    Signal engine, risk engine, execution layer: NOT IMPLEMENTED.
    Every signal passes — no gates evaluated.

    PDT NOTE: Slot arbitration removed June 4 2026. PDT rule eliminated.
    Intraday margin is the binding constraint — enforced by IBKR in real time.
    Same-day open+close is now always permitted. Early exits are immediate.
    Same-day spread rolls are a valid adjustment tactic (Phase 1+).
    """
    signal_id, was_duplicate = await db_insert_signal(pool, payload)

    logger.info(
        f"[{payload.system}] signal #{signal_id} | "
        f"{payload.ticker} {payload.direction} @ {payload.price} | "
        f"regime={payload.regime} ivr={payload.ivr} vix={payload.vix} | "
        f"intraday_flag={payload.intraday} was_duplicate={was_duplicate} | "
        f"PHASE 0 — engine NOT IMPLEMENTED, logged only"
    )

    return SignalResponse(
        signal_id=signal_id,
        system=payload.system,
        status="ACCEPTED",
        was_duplicate=was_duplicate,
        message="Phase 0 scaffold — signal logged to PostgreSQL, no execution",
    )


# ---------------------------------------------------------------------------
# Endpoints: Signal ingestion
# ---------------------------------------------------------------------------

@app.post("/raijin/signal", response_model=SignalResponse, tags=["signals"])
async def raijin_signal(payload: WebhookPayload):
    """Receive a RAIJIN webhook signal from the Cloudflare Worker."""
    validate_webhook_secret(payload.secret)
    if payload.system != "RAIJIN":
        raise HTTPException(422, "System mismatch: expected RAIJIN")
    return await process_signal(app.state.db, payload)


@app.post("/susanoo/signal", response_model=SignalResponse, tags=["signals"])
async def susanoo_signal(payload: WebhookPayload):
    """Receive a SUSANOO webhook signal from the Cloudflare Worker."""
    validate_webhook_secret(payload.secret)
    if payload.system != "SUSANOO":
        raise HTTPException(422, "System mismatch: expected SUSANOO")
    return await process_signal(app.state.db, payload)


# ---------------------------------------------------------------------------
# Endpoints: Control
# ---------------------------------------------------------------------------

@app.post("/halt", tags=["control"])
async def halt_all(x_api_key: str = Header(...)):
    """
    Emergency kill switch — freeze both systems.
    Phase 0: sets capital_fence is_frozen, logs to circuit_breaker_events.
    Does NOT cancel IBKR orders yet (execution layer Phase 1+).
    """
    validate_api_key(x_api_key)
    pool = app.state.db

    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE capital_fence
            SET is_frozen = TRUE,
                freeze_reason = 'Manual halt via /halt endpoint',
                updated_at = NOW()
            WHERE system IN ('RAIJIN', 'SUSANOO')
            """
        )
        await conn.execute(
            """
            INSERT INTO circuit_breaker_events
                (system, breaker_type, trigger_value, threshold_value, action_taken)
            VALUES
                (NULL, 'MANUAL_HALT', NULL, NULL,
                 'FULL_HALT — both systems frozen via /halt')
            """
        )

    logger.warning("🛑 MANUAL HALT — both systems frozen via /halt endpoint")
    return {
        "status": "HALTED",
        "systems": ["RAIJIN", "SUSANOO"],
        "ts": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/halt/{system}", tags=["control"])
async def halt_system(system: str, x_api_key: str = Header(...)):
    """Freeze a single system."""
    validate_api_key(x_api_key)
    system = system.upper()
    if system not in ("RAIJIN", "SUSANOO"):
        raise HTTPException(422, "system must be RAIJIN or SUSANOO")

    pool = app.state.db
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE capital_fence
            SET is_frozen = TRUE,
                freeze_reason = $1,
                updated_at = NOW()
            WHERE system = $2
            """,
            f"Manual halt via /halt/{system} endpoint",
            system,
        )
        await conn.execute(
            """
            INSERT INTO circuit_breaker_events
                (system, breaker_type, trigger_value, threshold_value, action_taken)
            VALUES ($1, 'MANUAL_HALT', NULL, NULL, $2)
            """,
            system,
            f"SYSTEM_HALT — {system} frozen via /halt/{system}",
        )

    logger.warning(f"🛑 MANUAL HALT — {system} frozen")
    return {
        "status": "HALTED",
        "system": system,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/resume/{system}", tags=["control"])
async def resume_system(system: str, x_api_key: str = Header(...)):
    """Unfreeze a system after manual review."""
    validate_api_key(x_api_key)
    system = system.upper()
    if system not in ("RAIJIN", "SUSANOO"):
        raise HTTPException(422, "system must be RAIJIN or SUSANOO")

    pool = app.state.db
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE capital_fence
            SET is_frozen = FALSE,
                freeze_reason = NULL,
                updated_at = NOW()
            WHERE system = $1
            """,
            system,
        )
        await conn.execute(
            """
            UPDATE circuit_breaker_events
            SET resolved_at = NOW(),
                notes = 'Manually resumed via /resume endpoint'
            WHERE system = $1
              AND breaker_type = 'MANUAL_HALT'
              AND resolved_at IS NULL
            """,
            system,
        )

    logger.info(f"✅ {system} resumed")
    return {
        "status": "RESUMED",
        "system": system,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Endpoints: Status and monitoring
# ---------------------------------------------------------------------------

@app.get("/status", tags=["monitoring"])
async def status():
    """
    Health check — no auth required.
    Returns server state, IBKR env, intraday trade counts, freeze state.

    NOTE: pdt_slots_remaining is not returned. PDT rule eliminated June 4 2026.
    Intraday margin is now the binding constraint (enforced by IBKR real-time).
    """
    pool = app.state.db
    try:
        intraday       = await db_intraday_summary(pool)
        raijin_frozen  = await db_is_frozen(pool, "RAIJIN")
        susanoo_frozen = await db_is_frozen(pool, "SUSANOO")
        open_positions = await db_count_open_positions(pool)
        db_ok          = True
    except Exception as e:
        logger.error(f"/status DB error: {e}")
        intraday = {}
        susanoo_frozen = raijin_frozen = open_positions = None
        db_ok = False

    return {
        "server":                "running",
        "version":               "0.3.0-phase0",
        "ibkr_env":              IBKR_ENV,
        "db_connected":          db_ok,
        "pdt_rule":              "ELIMINATED — June 4 2026 (FINRA Rule 4210)",
        "intraday_framework":    "ACTIVE — IBKR real-time margin engine",
        "intraday_trades_today": intraday.get("intraday_trades_today", 0),
        "raijin_intraday":       intraday.get("raijin_intraday", 0),
        "susanoo_intraday":      intraday.get("susanoo_intraday", 0),
        "raijin_frozen":         raijin_frozen,
        "susanoo_frozen":        susanoo_frozen,
        "open_positions":        open_positions,
        "uptime_seconds":        round(time.time() - _START_TIME, 1),
        "ts":                    datetime.now(timezone.utc).isoformat(),
    }


@app.get("/intraday", tags=["monitoring"])
async def intraday_status(x_api_key: str = Header(...)):
    """
    Intraday trade summary for the current session.

    Replaces /pdt — PDT rule eliminated June 4 2026.
    No slot limit exists. The binding constraint is IBKR's real-time
    intraday margin engine, not a trade count.

    Returns today's intraday trade count per system for monitoring purposes.
    Phase 1+: will add IBKR buying power headroom when execution layer is wired.
    """
    validate_api_key(x_api_key)
    pool = app.state.db
    summary = await db_intraday_summary(pool)
    return {
        **summary,
        "pdt_rule": "ELIMINATED — June 4 2026",
        "slot_limit": None,
        "note": (
            "No day-trade frequency limit. "
            "Same-day open+close permitted. "
            "Same-day spread rolls permitted (RAIJIN Phase 1). "
            "SUSANOO intraday gate available (Phase S1). "
            "Binding constraint: IBKR intraday margin."
        ),
    }


@app.get("/positions", tags=["monitoring"])
async def open_positions(x_api_key: str = Header(...)):
    """Current open positions across both systems."""
    validate_api_key(x_api_key)
    pool = app.state.db
    rows = await pool.fetch("SELECT * FROM v_open_positions ORDER BY entry_ts DESC")
    return [dict(r) for r in rows]


@app.get("/edge/summary", tags=["monitoring"])
async def edge_summary(x_api_key: str = Header(...)):
    """Edge Ledger performance summary from v_edge_performance."""
    validate_api_key(x_api_key)
    pool = app.state.db
    rows = await pool.fetch(
        "SELECT * FROM v_edge_performance ORDER BY system, edge_type"
    )
    return [dict(r) for r in rows]


@app.get("/is/summary", tags=["monitoring"])
async def is_summary(x_api_key: str = Header(...)):
    """Trailing IS ratio from v_trailing_is_ratio."""
    validate_api_key(x_api_key)
    pool = app.state.db
    rows = await pool.fetch("SELECT * FROM v_trailing_is_ratio")
    return [dict(r) for r in rows]


@app.get("/metrics/daily", tags=["monitoring"])
async def daily_metrics(x_api_key: str = Header(...)):
    """Last 30 days of daily_metrics for equity curve."""
    validate_api_key(x_api_key)
    pool = app.state.db
    rows = await pool.fetch(
        "SELECT * FROM daily_metrics ORDER BY trading_date DESC LIMIT 30"
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Global error handler
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception on {request.url}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500, content={"error": "Internal server error"}
    )
