"""
RAIJIN — Phase 0, Step 2
IBKR Live Account Connection Test
"""

import asyncio
import sys
import os
from ib_insync import IB, Stock, util
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

HOST      = os.getenv("IBKR_HOST", "127.0.0.1")
PORT      = int(os.getenv("IBKR_PORT", 7496))
CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", 1))


async def run():
    ib = IB()

    logger.info(f"Connecting to IBKR at {HOST}:{PORT} (clientId={CLIENT_ID})")
    try:
        await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=15)
    except Exception as e:
        logger.error(f"Connection failed: {e}")
        logger.info("Checklist:")
        logger.info("  — Is TWS open and fully loaded?")
        logger.info("  — Is 'Enable ActiveX and Socket Clients' checked?")
        logger.info("  — Is 'Read-Only API' UNCHECKED?")
        logger.info(f"  — Is port {PORT} correct? (Live TWS = 7496)")
        sys.exit(1)

    logger.success(f"✓ Connected. Server version: {ib.client.serverVersion}")

    # Account summary
    logger.info("Fetching account summary...")
    key_fields = {"NetLiquidation", "TotalCashValue", "BuyingPower", "AvailableFunds"}
    for av in ib.accountValues():
        if av.tag in key_fields and av.currency == "USD":
            logger.info(f"  {av.tag}: ${float(av.value):,.2f}")

    # SPY quote
    logger.info("Requesting SPY quote...")
    spy = Stock("SPY", "SMART", "USD")
    ib.qualifyContracts(spy)
    ticker = ib.reqMktData(spy, "", False, False)
    await asyncio.sleep(3)

    if ticker.last:
        logger.success(f"✓ SPY last: ${ticker.last:.2f} | bid: ${ticker.bid:.2f} | ask: ${ticker.ask:.2f}")
    else:
        logger.warning("SPY quote not received — market may be closed (normal outside hours)")

    ib.cancelMktData(spy)

    # Options chain
    logger.info("Requesting SPY options chain...")
    chains = ib.reqSecDefOptParams(spy.symbol, "", spy.secType, spy.conId)
    if chains:
        chain = next((c for c in chains if c.exchange == "SMART"), chains[0])
        expirations = sorted(chain.expirations)[:3]
        strikes_near_atm = sorted(
            chain.strikes,
            key=lambda s: abs(s - (ticker.last or 500))
        )[:5]
        logger.success("✓ Options chain received")
        logger.info(f"  Next 3 expirations: {expirations}")
        logger.info(f"  Strikes near ATM:   {strikes_near_atm}")
    else:
        logger.warning("Options chain not received — check market data subscriptions in IBKR account")

    logger.success("\n" + "═" * 50)
    logger.success("RAIJIN — IBKR CONNECTION TEST: PASSED")
    logger.success("═" * 50)

    ib.disconnect()


if __name__ == "__main__":
    util.startLoop()
    asyncio.run(run())
