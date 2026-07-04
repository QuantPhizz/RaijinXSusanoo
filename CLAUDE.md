# RAIJINXSUSANOO CLAUDE.md — Tier 3
# Owner: Nick (shugogeta)
# Location: ~/tko-agents/RaijinXSusanoo/CLAUDE.md
# Scope: All Claude Code sessions working inside this project
# Repo: https://github.com/QuantPhizz/RaijinXSusanoo (private, branch: main)
# Last Updated: July 4, 2026
#
# HIERARCHY POSITION:
#   Tier 1 (above): ~/.claude/CLAUDE.md          — machine-wide constitution
#   Tier 2 (above): ~/tko-agents/CLAUDE.md       — ecosystem layer
#   Tier 3 (this):  this file                    — project-specific only
#
# Everything in Tiers 1 and 2 is already in effect. This file adds to it.
# Nothing here overrides a higher tier. The Hallucination Directive
# (Tier 1 Section 2) applies with EXTRA force here — this project touches
# broker mechanics, FINRA rules, and options math. If uncertain, stop.

---

## SECTION 1 — WHAT THIS PROJECT IS

Two automated options-trading systems sharing one infrastructure layer:

  RAIJIN   — premium selling. Signal source: TradingView Pine mirror
             (RAIJIN/python-bot/scripts/raijin_signal_v1.pine).
  SUSANOO  — long premium. Dormant unless VRP < -3.0 (continuous sizing
             function, NOT a binary switch — see risk_parameters.yaml).

Current phase: Phase 0 (paper trading, E2E bring-up). ENV=paper everywhere.
IBKR live trading is Phase 1+. NEVER default IBKR_ENV to "live" — the code
enforces this; do not weaken it.

History note: this repo was split out of the local tko-agents working tree
on 2026-07-04 with a fresh single-commit history. The GitHub TKO-Agents
repo is a different project (agent platform); nothing here pushes there.

---

## SECTION 2 — LIVE TOPOLOGY (what breaks if you move things)

  TradingView alert (Pine webhook)
    → Cloudflare Worker "raijin-gateway" (RAIJIN/cloudflare-worker/,
      D1 database "raijin-signals")
    → shared Worker (shared/cloudflare-worker/, FASTAPI_URL var)
    → cloudflared named tunnel (runs as root homebrew service,
      public host: raijinxsusanoo-fastapi.quantftg.io → localhost:8000)
    → local FastAPI gateway (shared/api/server.py, uvicorn port 8000)
    → Supabase Postgres (DATABASE_URL in shared/api/.env)

Local server launch (cwd MUST be shared/api):
  cd ~/tko-agents/RaijinXSusanoo/shared/api
  ../../RAIJIN/python-bot/.venv/bin/uvicorn server:app \
      --host 0.0.0.0 --port 8000 --reload
Logs: RaijinXSusanoo/logs/uvicorn.log
Health check: curl localhost:8000/status  (and the tunnel URL above)

The tunnel and Workers reference URLs and ports, never local paths.
Moving files inside this repo requires only a uvicorn restart. Changing
the port, tunnel hostname, or Worker vars requires owner approval.

---

## SECTION 3 — DATABASE & MIGRATION DISCIPLINE

Schema-of-record: shared/db/migrations/, applied in order 001 → 002 → 003.
All migrations are idempotent. Never alter the live Supabase schema without
committing the migration here FIRST.

  001_schema.sql (v1.1)         — core schema. PDT rule was ELIMINATED by
                                  FINRA 2026-06-04: intraday_trades is
                                  observability only, NOT a gate. Do not
                                  reintroduce PDT/slot gating anywhere.
  002_outcome_resolved_ts.sql   — CPCV purge column. Skipping it silently
                                  biases Phase 5 PBO validation. Apply it.
  003_add_signal_provenance.sql — source/fidelity tags + signal_dedup_key.

Invariant: the dedup formula in server.py compute_dedup_key() and the
backfill SQL in migration 003 MUST stay identical. Changing one without
the other (and a recompute migration) corrupts idempotency. After applying
any migration, restart the uvicorn server.

Verification: shared/db/migrations/verify_phase0.sh (read-only; requires
DATABASE_URL sourced from shared/api/.env). Run it after any schema work.

---

## SECTION 4 — RISK ENGINE (locked parameters)

shared/risk/ and shared/config/risk_parameters.yaml are the frequency and
capital governors (Kelly tiers, circuit breakers, capital fence, VRP
sizing). These values are owner-locked: no agent changes them without
explicit per-session owner approval, and every change gets its own tagged
commit. The Kelly sizer + circuit breakers replaced PDT as the sole
frequency governors — treat any code path that blocks trades as
risk-engine territory.

---

## SECTION 5 — SECRETS

  - shared/api/.env and RAIJIN/python-bot/.env hold local secrets
    (DATABASE_URL, WEBHOOK_SECRET, API_KEY, INTERNAL_SECRET, POLYGON /
    FLASHALPHA keys). Both are gitignored. Never commit, print, or echo
    their contents.
  - Cloudflare Worker secrets are set via `wrangler secret put` only —
    never in wrangler.toml. The [vars] blocks (URLs, ENV) are config,
    not secrets. D1 database IDs are not secrets.
  - The Pine script placeholder RAIJIN_SECRET_CHANGE_ME stays a
    placeholder in git; the real value lives in TradingView + Worker
    secret only.

---

## SECTION 6 — WORKING AGREEMENTS

  - Commits: [module] short description (Tier 1 convention). This history
    is the invention timeline — commit at decision points.
  - requirements.txt (repo root) = server/risk deps;
    RAIJIN/python-bot/requirements.txt = bot deps. Both install into
    RAIJIN/python-bot/.venv (python 3.12).
  - Worker deploys: npm scripts in package.json (worker:deploy,
    raijin:worker:deploy).
  - Anything touching money movement, order routing, broker API
    capabilities, or FINRA/regulatory questions: Hallucination Directive,
    full stop — verify against IBKR/FINRA docs or say "I don't have a
    confirmed answer for this."
