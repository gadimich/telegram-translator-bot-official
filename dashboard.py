"""Admin dashboard — served alongside the bot on PORT."""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone

import asyncpg
import httpx
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

app = FastAPI(docs_url=None, redoc_url=None)
security = HTTPBasic()

BOT_TOKEN = os.environ["BOT_TOKEN"]
STARS_TO_USD = 1 / 77  # ~$0.013/star (what users paid; Telegram takes 30%)


def require_auth(credentials: HTTPBasicCredentials = Depends(security)):
    password = os.getenv("DASHBOARD_PASSWORD", "admin")
    ok = secrets.compare_digest(credentials.password.encode(), password.encode())
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"},
        )


@app.get("/", response_class=HTMLResponse)
async def dashboard(_: None = Depends(require_auth)):
    pool: asyncpg.Pool = app.state.pool
    period = datetime.now(timezone.utc).strftime("%Y-%m")

    total_users = await pool.fetchval("SELECT COUNT(*) FROM user_prefs") or 0
    paying_users = await pool.fetchval(
        "SELECT COUNT(*) FROM user_prefs WHERE plan IN ('basic','pro') AND plan_until > NOW()"
    ) or 0
    msgs_month = await pool.fetchval(
        "SELECT COALESCE(SUM(count),0) FROM usage WHERE period=$1", period
    ) or 0
    cost_month = await pool.fetchval(
        "SELECT COALESCE(SUM(cost_usd),0) FROM usage WHERE period=$1", period
    ) or 0.0
    cost_total = await pool.fetchval(
        "SELECT COALESCE(SUM(cost_usd),0) FROM usage"
    ) or 0.0
    rev_month_stars = await pool.fetchval(
        "SELECT COALESCE(SUM(stars),0) FROM payments WHERE paid_at >= date_trunc('month', NOW())"
    ) or 0
    rev_total_stars = await pool.fetchval(
        "SELECT COALESCE(SUM(stars),0) FROM payments"
    ) or 0

    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMyStarBalance")
    stars_balance = r.json().get("result", {}).get("amount", 0)

    rev_month_usd  = rev_month_stars * STARS_TO_USD
    rev_total_usd  = rev_total_stars * STARS_TO_USD
    profit_month   = rev_month_usd - cost_month
    profit_total   = rev_total_usd - cost_total

    users = await pool.fetch("""
        SELECT
            p.user_id,
            p.first_name,
            p.username,
            p.source_lang,
            p.target_lang,
            COALESCE(p.plan, 'free') AS plan,
            p.plan_until,
            COALESCE(u_mo.count, 0)    AS msgs_month,
            COALESCE(u_mo.cost_usd, 0) AS cost_month,
            COALESCE(u_all.total, 0)   AS msgs_total,
            COALESCE(u_all.total_cost, 0) AS cost_total,
            COALESCE(pay.total_stars, 0)  AS revenue_stars
        FROM user_prefs p
        LEFT JOIN usage u_mo ON u_mo.user_id = p.user_id AND u_mo.period = $1
        LEFT JOIN (
            SELECT user_id, SUM(count) AS total, SUM(cost_usd) AS total_cost
            FROM usage GROUP BY user_id
        ) u_all ON u_all.user_id = p.user_id
        LEFT JOIN (
            SELECT user_id, SUM(stars) AS total_stars FROM payments GROUP BY user_id
        ) pay ON pay.user_id = p.user_id
        ORDER BY msgs_month DESC, msgs_total DESC
    """, period)

    def smart_usd(v: float) -> str:
        if abs(v) < 0.001:
            return f"${v:.5f}"
        if abs(v) < 0.01:
            return f"${v:.4f}"
        return f"${v:.2f}"

    def fmt_usd(v: float) -> str:
        if v == 0:
            return '<span class="text-gray-300">—</span>'
        color = "text-emerald-500" if v > 0 else "text-red-400"
        sign = "-" if v < 0 else ""
        return f'<span class="{color} font-medium">{sign}{smart_usd(abs(v))}</span>'

    def fmt_cost(v: float) -> str:
        if v == 0:
            return '<span class="text-gray-300">—</span>'
        return f'<span class="text-red-400">{smart_usd(v)}</span>'

    plan_badge = {
        "free":  '<span class="px-2 py-0.5 text-xs rounded-full bg-gray-100 text-gray-500 font-medium">Free</span>',
        "basic": '<span class="px-2 py-0.5 text-xs rounded-full bg-blue-100 text-blue-700 font-medium">Basic</span>',
        "pro":   '<span class="px-2 py-0.5 text-xs rounded-full bg-purple-100 text-purple-700 font-medium">Pro</span>',
    }

    rows = ""
    for u in users:
        name  = u["first_name"] or "—"
        uname = (f'<a href="https://t.me/{u["username"]}" class="text-blue-500 hover:underline" target="_blank">'
                 f'@{u["username"]}</a>') if u["username"] else '<span class="text-gray-300">—</span>'
        src = u["source_lang"] or ""
        tgt = u["target_lang"] or ""
        langs = f"{src} → {tgt}" if src and tgt else (tgt or src or "—")
        plan  = u["plan"] or "free"
        badge = plan_badge.get(plan, plan_badge["free"])
        until = u["plan_until"].strftime("%-d %b %Y") if u["plan_until"] else '<span class="text-gray-300">—</span>'
        rev   = u["revenue_stars"] * STARS_TO_USD
        net   = rev - u["cost_total"]

        rows += f"""
        <tr class="border-t border-gray-50 hover:bg-gray-50">
          <td class="px-5 py-3 font-medium text-gray-800">{name}</td>
          <td class="px-5 py-3">{uname}</td>
          <td class="px-5 py-3">{badge}</td>
          <td class="px-5 py-3 text-gray-500 text-xs">{langs}</td>
          <td class="px-5 py-3 text-center font-medium">{u['msgs_month']}</td>
          <td class="px-5 py-3 text-center text-gray-400">{u['msgs_total']}</td>
          <td class="px-5 py-3 text-right">{fmt_usd(rev) if rev else '<span class="text-gray-300">—</span>'}</td>
          <td class="px-5 py-3 text-right">{fmt_cost(u['cost_month'])}</td>
          <td class="px-5 py-3 text-right">{fmt_cost(u['cost_total'])}</td>
          <td class="px-5 py-3 text-right">{fmt_usd(net)}</td>
          <td class="px-5 py-3 text-gray-400 text-xs">{until}</td>
        </tr>"""

    def card(label, value, sub="", value_class="text-gray-800"):
        return f"""
        <div class="bg-white rounded-2xl p-5 shadow-sm border border-gray-100">
          <div class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">{label}</div>
          <div class="text-3xl font-bold {value_class}">{value}</div>
          {"" if not sub else f'<div class="text-xs text-gray-400 mt-1">{sub}</div>'}
        </div>"""

    def s_usd(v):
        if abs(v) < 0.001: return f"${v:.5f}"
        if abs(v) < 0.01:  return f"${v:.4f}"
        return f"${v:.2f}"

    profit_class = "text-emerald-500" if profit_month >= 0 else "text-red-400"
    profit_total_class = "text-emerald-500" if profit_total >= 0 else "text-red-400"
    updated = datetime.now(timezone.utc).strftime("%-d %b %Y %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Voice Translator — Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-50 min-h-screen font-sans">
<div class="max-w-7xl mx-auto px-6 py-8">

  <div class="mb-8">
    <h1 class="text-2xl font-bold text-gray-900">🎙 Voice Translator</h1>
    <p class="text-sm text-gray-400 mt-0.5">Admin Dashboard · Updated {updated} UTC</p>
  </div>

  <div class="grid grid-cols-3 gap-4 mb-4">
    {card("Total Users", total_users)}
    {card("Paying Users", paying_users, value_class="text-blue-500")}
    {card("Msgs This Month", msgs_month)}
  </div>

  <div class="grid grid-cols-3 gap-4 mb-4">
    {card("Revenue This Month", f"${rev_month_usd:.2f}", f"{rev_month_stars:,} Stars received", "text-green-500")}
    {card("Revenue Total", f"${rev_total_usd:.2f}", f"{rev_total_stars:,} Stars all time", "text-green-600")}
    {card("Stars Balance", f"⭐ {stars_balance:,}", "not yet withdrawn", "text-yellow-400")}
  </div>

  <div class="grid grid-cols-3 gap-4 mb-8">
    {card("OpenAI Cost This Month", s_usd(cost_month), value_class="text-red-400")}
    {card("OpenAI Cost Total", s_usd(cost_total), value_class="text-red-400")}
    {card("Net Profit This Month", s_usd(profit_month), "revenue − OpenAI cost", profit_class)}
  </div>

  <div class="bg-white rounded-2xl shadow-sm border border-gray-100 overflow-hidden">
    <div class="px-6 py-4 border-b border-gray-100">
      <h2 class="text-sm font-semibold text-gray-700">Users</h2>
    </div>
    <div class="overflow-x-auto">
    <table class="w-full text-sm">
      <thead>
        <tr class="text-left text-xs font-medium text-gray-400 uppercase tracking-wide bg-gray-50">
          <th class="px-5 py-3">Name</th>
          <th class="px-5 py-3">Username</th>
          <th class="px-5 py-3">Plan</th>
          <th class="px-5 py-3">Languages</th>
          <th class="px-5 py-3 text-center">Msgs (mo)</th>
          <th class="px-5 py-3 text-center">Msgs (total)</th>
          <th class="px-5 py-3 text-right">Revenue</th>
          <th class="px-5 py-3 text-right">OAI Cost (mo)</th>
          <th class="px-5 py-3 text-right">OAI Cost (total)</th>
          <th class="px-5 py-3 text-right">Net Profit</th>
          <th class="px-5 py-3">Plan Until</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    </div>
  </div>

</div>
</body>
</html>"""
    return HTMLResponse(content=html)
