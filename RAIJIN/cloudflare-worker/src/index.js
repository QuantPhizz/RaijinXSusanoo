export default {
  async fetch(request, env) {
    // Only accept POST
    if (request.method !== "POST") {
      return new Response(JSON.stringify({ error: "Method not allowed" }), {
        status: 405,
        headers: { "Content-Type": "application/json" },
      });
    }

    let body;
    try {
      body = await request.json();
    } catch (e) {
      return new Response(JSON.stringify({ error: "Invalid JSON" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    // Validate webhook secret
    if (!body.secret || body.secret !== env.WEBHOOK_SECRET) {
      return new Response(JSON.stringify({ error: "Unauthorized" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      });
    }

    // Generate signal ID
    const signalId = `sig-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const receivedAt = Date.now();

    // Extract fields from payload
    const {
      action,
      ticker,
      strategy,
      timeframe,
      price,
      atr,
      rsi,
      regime,
      ivr,
      meta,
    } = body;

    const volRatio = meta?.vol_ratio ?? null;

    // Log to D1
    try {
      await env.DB.prepare(
        `INSERT INTO signals (id, received_at, action, ticker, strategy, timeframe, price, atr, rsi, regime, ivr, vol_ratio)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
      )
        .bind(signalId, receivedAt, action, ticker, strategy, timeframe, price, atr, rsi, regime, ivr, volRatio)
        .run();
    } catch (e) {
      console.error("D1 insert failed:", e.message);
      return new Response(JSON.stringify({ error: "DB write failed", detail: e.message }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      });
    }

    // Forward to Python bot
    let forwardStatus = 0;
    try {
      const forwardPayload = {
        id: signalId,
        receivedAt,
        action,
        ticker,
        strategy,
        timeframe,
        price,
        atr,
        rsi,
        regime,
        ivr,
        volRatio,
        source: "tradingview",
      };

      const botResponse = await fetch(env.BOT_URL, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Internal-Secret": env.INTERNAL_SECRET,
        },
        body: JSON.stringify(forwardPayload),
      });

      forwardStatus = botResponse.status;
    } catch (e) {
      console.error("Forward to bot failed:", e.message);
      forwardStatus = 0;
    }

    // Update forward status in D1
    try {
      await env.DB.prepare(
        `UPDATE signals SET forwarded = 1, forward_status = ? WHERE id = ?`
      )
        .bind(forwardStatus, signalId)
        .run();
    } catch (e) {
      console.error("D1 update failed:", e.message);
    }

    return new Response(
      JSON.stringify({
        ok: true,
        id: signalId,
        action,
        ticker,
        forwarded: forwardStatus,
        env: env.ENV,
      }),
      {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }
    );
  },
};
