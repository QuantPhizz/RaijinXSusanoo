export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/health" && request.method === "GET") {
      return jsonResponse({
        status:   "ok",
        version:  "2.0.0",
        worker:   "raijin-susanoo-gateway",
        env:      env.ENV ?? "unknown",
        pdt_rule: "ELIMINATED — June 4 2026",
      }, 200);
    }

    if (url.pathname === "/webhook" && request.method === "POST") {
      return await handleWebhook(request, env);
    }

    return jsonResponse({ error: "Not found" }, 404);
  }
};

async function handleWebhook(request, env) {
  let payload;
  try {
    payload = await request.json();
  } catch (e) {
    return jsonResponse({ error: "Invalid JSON" }, 400);
  }

  if (!payload.secret || payload.secret !== env.WEBHOOK_SECRET) {
    return jsonResponse({ error: "Unauthorized" }, 401);
  }

  const system = (payload.system || "").toUpperCase();
  if (system !== "RAIJIN" && system !== "SUSANOO") {
    return jsonResponse({ error: "Invalid system — must be RAIJIN or SUSANOO" }, 422);
  }

  const required = ["ticker", "direction", "price", "timestamp"];
  for (const field of required) {
    if (payload[field] === undefined || payload[field] === null || payload[field] === "") {
      return jsonResponse({ error: `Missing required field: ${field}` }, 422);
    }
  }

  const direction = (payload.direction || "").toUpperCase();
  if (direction !== "BUY" && direction !== "SELL") {
    return jsonResponse({ error: "Invalid direction — must be BUY or SELL" }, 422);
  }

  let d1Id = null;
  try {
    const result = await env.DB.prepare(
      `INSERT INTO signal_log (system, ticker, direction, price, raw_payload)
       VALUES (?, ?, ?, ?, ?)`
    ).bind(system, payload.ticker, direction, payload.price, JSON.stringify(payload)).run();
    d1Id = result.meta?.last_row_id ?? null;
  } catch (e) {
    console.error("D1 write failed (non-fatal):", e.message);
  }

  const endpoint = system === "RAIJIN"
    ? `${env.FASTAPI_URL}/raijin/signal`
    : `${env.FASTAPI_URL}/susanoo/signal`;

  let forwardStatus = 0;
  let responseBody  = {};

  try {
    const forwardResp = await fetch(endpoint, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(payload),
    });
    forwardStatus = forwardResp.status;
    responseBody  = await forwardResp.json().catch(() => ({}));

    if (d1Id !== null) {
      try {
        await env.DB.prepare(
          `UPDATE signal_log SET forwarded = 1, forward_status_code = ? WHERE id = ?`
        ).bind(forwardStatus, d1Id).run();
      } catch (_) {}
    }

    return jsonResponse(responseBody, forwardStatus);

  } catch (e) {
    console.error("FastAPI forward failed:", e.message);
    if (d1Id !== null) {
      try {
        await env.DB.prepare(
          `UPDATE signal_log SET forwarded = 0, forward_status_code = -1 WHERE id = ?`
        ).bind(d1Id).run();
      } catch (_) {}
    }
    return jsonResponse({
      error:  "FastAPI unreachable — signal logged to D1 for recovery",
      system: system,
      ticker: payload.ticker,
      d1_id:  d1Id,
    }, 502);
  }
}

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
