# SERVER PATCH 003 — Populate provenance + idempotent insert

**Applies to:** `shared/api/server.py` (post-Claude-Code v0.2.0-phase0 state)
**Depends on:** Migration `003_add_signal_provenance.sql` (must be applied first)
**Session:** Phase 0 Step 4 — Option A (E2 closure)

---

## What this patch does

Three changes to `server.py`, in dependency order:

1. **Add `hashlib` import** (may already be present — check first)
2. **Add a `compute_dedup_key()` helper** for deterministic hashing that matches the SQL backfill in migration 003
3. **Rewrite the `signals` INSERT** to populate `source`, `fidelity`, `signal_dedup_key` and handle `ON CONFLICT` idempotently

Version bump `__version__` from `"0.2.0-phase0"` to `"0.3.0-phase0"` so the version guard catches whether the patched code is actually running.

---

## Change 1 — Imports

**Find near the top of `server.py`, the imports block:**

```python
import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
```

**Add `import hashlib` if not already present.** Alphabetical order is fine but not required:

```python
import hashlib
import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
```

**Verify:** `grep -n "^import hashlib" shared/api/server.py` returns one match.

---

## Change 2 — Version bump

**Find the `__version__` assignment or the FastAPI app instantiation:**

```python
__version__ = "0.2.0-phase0"
```

**or inside the FastAPI() call:**

```python
app = FastAPI(
    title="TKO-Agents",
    version="0.2.0-phase0",
    ...
)
```

**Change both to:**

```python
__version__ = "0.3.0-phase0"
```

```python
app = FastAPI(
    title="TKO-Agents",
    version="0.3.0-phase0",
    ...
)
```

**Verify:** `python -c "import server; assert server.__version__ == '0.3.0-phase0'; print('OK')"` from `shared/api/`.

---

## Change 3 — Dedup key helper

**Add this function anywhere between imports and the FastAPI app instantiation.** Suggested placement: right after the env-var block, before `_START_TIME`.

```python
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
    timestamp: "datetime",
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
```

**Verify:** `python -c "from server import compute_dedup_key; from datetime import datetime, timezone; print(compute_dedup_key('RAIJIN','SPY','momentum_v1','BUY',datetime(2026,7,3,22,45,0,tzinfo=timezone.utc)))"` returns a 64-character hex string.

---

## Change 4 — INSERT rewrite

**Find the current INSERT for `signals`.** It likely looks something like:

```python
row = await conn.fetchrow(
    """
    INSERT INTO signals (
        system, ticker, direction, tv_price, tv_atr, tv_rsi,
        tv_regime, raw_payload, status
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    RETURNING id
    """,
    payload.system, payload.ticker, payload.direction,
    payload.price, payload.atr, payload.rsi, payload.regime,
    json.dumps(payload.model_dump(mode="json")),
    "ACCEPTED",
)
signal_id = row["id"]
```

**Replace with:**

```python
# Compute idempotency key at ingress, before insert
dedup_key = compute_dedup_key(
    system=payload.system,
    ticker=payload.ticker,
    strategy=(payload.model_dump().get("strategy")),  # optional field
    direction=payload.direction,
    timestamp=payload.timestamp,
)

# Provenance tags for E2 fidelity guard
# Phase 0: signals originate directly from TradingView Pine, no gate
# evaluation has run. source reflects origin, fidelity is UNKNOWN because
# no data provider was consulted for this signal.
signal_source = "TRADINGVIEW_PINE"
signal_fidelity = "UNKNOWN"

# Idempotent INSERT: on dedup collision, return the existing signal_id
# unchanged. Two round trips in the collision case (rare) — clarity over
# cleverness. See Approach A discussion in patch notes.
row = await conn.fetchrow(
    """
    INSERT INTO signals (
        system, ticker, direction, tv_price, tv_atr, tv_rsi,
        tv_regime, raw_payload, status,
        source, fidelity, signal_dedup_key
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
    ON CONFLICT (signal_dedup_key) DO NOTHING
    RETURNING id
    """,
    payload.system.upper(),
    payload.ticker.upper(),
    payload.direction.upper(),
    payload.price, payload.atr, payload.rsi, payload.regime,
    json.dumps(payload.model_dump(mode="json")),
    "ACCEPTED",
    signal_source,
    signal_fidelity,
    dedup_key,
)

was_duplicate = False
if row is None:
    # ON CONFLICT hit — fetch the existing row's id so TV's retry sees success
    row = await conn.fetchrow(
        "SELECT id FROM signals WHERE signal_dedup_key = $1",
        dedup_key,
    )
    was_duplicate = True
    logger.info(f"[dedup] retry detected, returning existing signal_id={row['id']}")

signal_id = row["id"]
```

**Also update the response body to include the duplicate flag** (optional but useful for TV debugging):

```python
return JSONResponse({
    "signal_id": signal_id,
    "system": payload.system.upper(),
    "status": "ACCEPTED",
    "was_duplicate": was_duplicate,
    "message": "Phase 0 scaffold — signal logged to PostgreSQL, no execution",
})
```

**Notes on the normalization:**

- **`.upper()` on system/ticker/direction** before insert AND in dedup key. Pine emits lowercase `"buy"` today; schema expects uppercase `"BUY"`. If server.py was silently accepting lowercase and inserting it, that would eventually cause a semantic mismatch with downstream code that filters `WHERE direction = 'BUY'`. This patch normalizes at ingress.
- **`payload.timestamp.isoformat()`** in the dedup key. Pydantic v2 parses the ISO8601 string into a `datetime` object; `.isoformat()` renders it back to a canonical string. This means `2026-07-03T22:45:00+00:00` and `2026-07-03T22:45:00Z` (both valid ISO8601 UTC representations of the same moment) produce different dedup keys — because `isoformat()` renders whatever offset the datetime was parsed with. If this becomes a problem later (unlikely with Pine as the only source), normalize to UTC first: `timestamp.astimezone(timezone.utc).isoformat()`.

---

## Deployment sequence

**Order matters.** Do these in exactly this order, in one sitting.

### Step 1 — Apply the migration

Copy `003_add_signal_provenance.sql` into your repo:

```bash
cp /path/to/downloaded/003_add_signal_provenance.sql \
   /Users/shugogeta/tko-agents/RaijinXSusanoo/shared/db/migrations/
```

Apply via Supabase SQL Editor:
1. Open Supabase Dashboard → SQL Editor → New query
2. Paste the entire content of `003_add_signal_provenance.sql`
3. Click Run
4. Confirm the transaction returned `COMMIT` without errors

### Step 2 — Verify the migration landed

Paste each of the four verification queries from the bottom of the migration file into the SQL Editor and confirm expected results:

```sql
-- 1. Existing rows populated
SELECT id, source, fidelity, signal_dedup_key FROM signals ORDER BY id;
-- Expect: 2 rows, all three new fields non-null.
-- source='SYNTHETIC_CURL', fidelity='UNKNOWN', dedup_key=<64 hex chars>

-- 2. Constraints exist
SELECT conname, contype FROM pg_constraint
WHERE conrelid = 'signals'::regclass
  AND conname IN ('signals_fidelity_check', 'signals_dedup_key_unique');
-- Expect: 2 rows

-- 3. No duplicate dedup keys
SELECT signal_dedup_key, COUNT(*) FROM signals
GROUP BY signal_dedup_key HAVING COUNT(*) > 1;
-- Expect: 0 rows

-- 4. All fidelity values valid
SELECT fidelity, COUNT(*) FROM signals GROUP BY fidelity;
-- Expect: fidelity='UNKNOWN', count=2
```

**If any verification fails, STOP.** Do not proceed to Step 3. Roll back with the rollback block at the bottom of the migration file, diagnose, retry.

### Step 3 — Apply server.py changes

Make Changes 1, 2, 3, 4 above in `shared/api/server.py`. Save.

### Step 4 — Restart uvicorn

In the terminal running uvicorn: Ctrl+C, then restart:

```bash
cd /Users/shugogeta/tko-agents/RaijinXSusanoo/shared/api
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

Watch for clean startup: `application startup complete` with no tracebacks.

**Note on `--reload`:** in theory, `--reload` should have picked up the changes automatically. In practice, `--reload` can miss changes to helper functions or fail to hot-swap import structure. Explicit restart is safer.

### Step 5 — Verify version bump

New terminal:

```bash
curl -sS localhost:8000/status | python -m json.tool | grep version
```

Expect: `"version": "0.3.0-phase0"`. If it still shows `0.2.0-phase0`, uvicorn didn't pick up the change — force restart.

### Step 6 — Synthetic verification (both idempotency legs)

**First fire — expect fresh insert:**

```bash
SECRET=$(grep '^WEBHOOK_SECRET=' /Users/shugogeta/tko-agents/RaijinXSusanoo/shared/api/.env | cut -d= -f2- | tr -d '"'"'"'')
curl -X POST https://raijin-susanoo-gateway.nickphizycs.workers.dev/webhook \
  -H "Content-Type: application/json" \
  -d "{\"system\":\"RAIJIN\",\"secret\":\"$SECRET\",\"ticker\":\"SPY\",\"direction\":\"BUY\",\"price\":450.00,\"atr\":2.5,\"rsi\":55,\"regime\":\"trending\",\"strategy\":\"patch_003_verify\",\"timestamp\":\"2026-07-04T14:00:00+00:00\"}"
```

Expect: 200 with `"signal_id":3`, `"was_duplicate":false`.

**Second fire — same payload, expect duplicate hit:**

Rerun the exact same curl. Expect: 200 with `"signal_id":3` (same id!), `"was_duplicate":true`. Also expect a log line in uvicorn: `[dedup] retry detected, returning existing signal_id=3`.

**Verify in Supabase:**

```sql
SELECT id, system, ticker, direction, tv_price, source, fidelity,
       LEFT(signal_dedup_key, 12) AS dedup_prefix, status,
       raw_payload->>'timestamp' AS pine_timestamp
FROM signals
ORDER BY id DESC
LIMIT 5;
```

Expect: exactly one new row (id=3) with `source='TRADINGVIEW_PINE'`, `fidelity='UNKNOWN'`, a dedup prefix. If two rows for the same payload landed, the ON CONFLICT clause is broken — investigate.

`unset SECRET` when done.

### Step 7 — TradingView alert still armed

E1 is now safer than it was before this patch. If BTCUSD fires while you're mid-patch, the alert still delivers to the Worker → FastAPI, and the new insert code populates the provenance columns. Nothing you're doing invalidates the armed alert.

If it fires *during* the migration transaction, the INSERT will block until the migration COMMITs (transactions are serializable), then land normally. Postgres handles this cleanly.

If it fires between Step 4 (server restart) and being back at the prompt — same thing. The new server code processes it. You'll see `signal_id: 3` (or later) with real Pine data instead of your synthetic verification signal.

---

## Post-patch state

After Steps 1–6, the system state:

- **Schema:** `signals` has `source` (TEXT NOT NULL), `fidelity` (TEXT NOT NULL CHECK), `signal_dedup_key` (TEXT NOT NULL UNIQUE). Existing rows backfilled.
- **Server:** v0.3.0-phase0, populating provenance at insert, idempotent on duplicate dedup keys.
- **YAML fidelity guard:** now enforceable. `dormancy_locked_pending_fidelity: PRODUCTION` predicates on a real column.
- **E1:** still armed on BTCUSD. When it fires, the row lands fully provenance-tagged and dedup-protected.

**E2 checkpoint of the session spec** — writable as truthful GREEN after E1 lands and the row shows `source='TRADINGVIEW_PINE'`, `fidelity='UNKNOWN'`, non-null dedup_key.

---

## Dominant failure modes

**1. Migration applied without server patch.** Old server.py doesn't populate `source`/`fidelity`/`signal_dedup_key`. First insert fails with `NOT NULL violation on signal_dedup_key`. Server logs the traceback, TV sees 500, TV retries, retry also fails. Effectively the signal path is broken until server.py is patched.
- **Mitigation:** ALWAYS ship migration and server patch as a single deployment. Do not commit the migration to git without the server patch in the same commit.

**2. Server patched but migration not applied.** New INSERT tries to write columns that don't exist. Postgres returns error. Server logs, TV sees 500.
- **Mitigation:** run migration FIRST, verify with the four queries, THEN patch server.

**3. Version bump forgotten.** `__version__` still says `0.2.0-phase0`. All the new code runs correctly, but the version guard can't distinguish. Cosmetic but confusing at diagnostic time.
- **Mitigation:** Change 2 above. Confirm with `curl /status | grep version`.

**4. Pine emits lowercase, dedup key computed on lowercase, DB stores uppercase — semantic drift.** Currently mitigated by `.upper()` in both the INSERT and the `compute_dedup_key()` function. If a future change removes `.upper()` from one but not the other, dedup keys stop matching across identical events.
- **Mitigation:** the normalization comment in `compute_dedup_key()`. Add a unit test that asserts `compute_dedup_key('raijin','spy','x','buy',ts) == compute_dedup_key('RAIJIN','SPY','x','BUY',ts)`.

**5. `payload.strategy` accessed as attribute vs `.model_dump().get('strategy')`.** If `strategy` isn't declared on the `WebhookPayload` model, `payload.strategy` throws `AttributeError`. The patch uses `payload.model_dump().get('strategy')` which is safe. If you'd rather add `strategy: str | None = None` to the Pydantic model instead, that's cleaner — but requires an extra field declaration.
- **Mitigation:** whichever approach, be consistent. Grep the codebase for other references to `payload.strategy` and normalize.

---

## Rollback

If any step 3–6 fails and can't be resolved in <15 min: revert server.py to `0.2.0-phase0` (git checkout previous version), restart uvicorn. The migration itself does not need to be rolled back — the columns default to `'UNKNOWN'`, old inserts still work. But the dedup UNIQUE constraint on a NOT NULL column requires SOME value at insert, and the old server won't provide one.

To roll back the migration itself, use the rollback block at the bottom of `003_add_signal_provenance.sql`.

**In practice:** don't rush this. If steps break, take the time to diagnose. The system is in the "waiting on E1" state anyway — nothing is under time pressure.
