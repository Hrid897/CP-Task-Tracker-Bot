import os
import json
import asyncio
from datetime import datetime, time
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from aiohttp import web

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_TOKEN      = os.environ["DISCORD_TOKEN"]
SUMMARY_CHANNEL_ID = int(os.environ["SUMMARY_CHANNEL_ID"])
TZ                 = ZoneInfo("Asia/Dhaka")
DATA_FILE          = "data.json"
TASKS_FILE         = "tasks.json"

# ── Data helpers ──────────────────────────────────────────────────────────────
def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default

def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)

def today_key():
    return datetime.now(TZ).strftime("%Y-%m-%d")

# ── Discord client ────────────────────────────────────────────────────────────
intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)

# ── Build summary embed ───────────────────────────────────────────────────────
def build_summary_embed(date_key=None):
    date_key   = date_key or today_key()
    data       = load_json(DATA_FILE, {})
    tasks_data = load_json(TASKS_FILE, {})

    day_tasks    = tasks_data.get(date_key, [])   # list of {id, title}
    day_progress = data.get(date_key, {})          # {username: {task_id: bool}}
    total_tasks  = len(day_tasks)

    buckets = {}   # pct_bucket -> list of user summary strings

    for username, completed in day_progress.items():
        done  = sum(1 for t in day_tasks if completed.get(t["id"], False))
        pct   = round((done / total_tasks * 100) if total_tasks else 0)
        # Round to nearest 50 for grouping, but keep real pct for display
        label = f"`{username}` — {done}/{total_tasks} tasks ({pct}%)"
        buckets.setdefault(pct, []).append((done, label))

    embed = discord.Embed(
        title="📊 CP Daily Checklist — Summary",
        description=f"📅 **{date_key}**  |  🌏 Asia/Dhaka",
        color=0x7c4dff
    )

    if not day_tasks:
        embed.add_field(name="⚠️ No tasks set today", value="Admin has not set tasks yet.", inline=False)
        embed.set_footer(text="Road to 2★ | CP Daily Checklist")
        return embed

    if not day_progress:
        embed.add_field(name="No submissions yet", value="No one has submitted tasks today.", inline=False)
        embed.set_footer(text="Road to 2★ | CP Daily Checklist")
        return embed

    # Sort buckets highest % first
    for pct in sorted(buckets.keys(), reverse=True):
        entries = buckets[pct]
        lines   = "\n".join(line for _, line in sorted(entries, reverse=True))
        if pct == 100:
            heading = f"🏆 100% Completed ({len(entries)})"
        elif pct == 0:
            heading = f"😴 0% Completed ({len(entries)})"
        else:
            heading = f"⚡ {pct}% Completed ({len(entries)})"
        embed.add_field(name=heading, value=lines, inline=False)

    # Today's tasks list
    task_list = "\n".join(f"• {t['title']}" for t in day_tasks)
    embed.add_field(name="📋 Today's Tasks", value=task_list, inline=False)

    total_users = len(day_progress)
    full_done   = sum(1 for u, c in day_progress.items()
                      if all(c.get(t["id"], False) for t in day_tasks))
    overall_pct = round((full_done / total_users * 100) if total_users else 0)

    embed.set_footer(
        text=f"Active users: {total_users} | Full completion: {overall_pct}% | Road to 2★"
    )
    embed.timestamp = datetime.now(TZ)
    return embed

# ── Slash command: /summary ───────────────────────────────────────────────────
@tree.command(name="summary", description="Show today's CP checklist summary")
async def summary_cmd(interaction: discord.Interaction):
    embed = build_summary_embed()
    await interaction.response.send_message(embed=embed)

# ── Scheduled daily summary at 23:59 Dhaka time ──────────────────────────────
@tasks.loop(time=time(23, 59, 0, tzinfo=TZ))
async def daily_summary():
    channel = client.get_channel(SUMMARY_CHANNEL_ID)
    if channel:
        embed = build_summary_embed()
        await channel.send("🔔 **Daily Summary — 11:59 PM**", embed=embed)

# ── CORS helper ───────────────────────────────────────────────────────────────
def cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

async def handle_options(request):
    return cors(web.Response(status=204))

# ── POST /admin/tasks — save today's tasks ────────────────────────────────────
async def handle_set_tasks(request):
    try:
        body     = await request.json()
        date_key = body.get("date") or today_key()
        tasks    = body.get("tasks", [])   # [{id, title, description}]

        if not isinstance(tasks, list):
            return cors(web.Response(status=400, text="tasks must be a list"))

        all_tasks = load_json(TASKS_FILE, {})
        all_tasks[date_key] = tasks
        save_json(TASKS_FILE, all_tasks)

        return cors(web.Response(status=200, text="OK"))
    except Exception as e:
        print("set_tasks error:", e)
        return cors(web.Response(status=500, text="Server error"))

# ── GET /tasks?date=YYYY-MM-DD — fetch tasks for a day ───────────────────────
async def handle_get_tasks(request):
    date_key  = request.rel_url.query.get("date") or today_key()
    all_tasks = load_json(TASKS_FILE, {})
    tasks     = all_tasks.get(date_key, [])
    return cors(web.json_response({"date": date_key, "tasks": tasks}))

# ── POST /task — receive task completion from HTML ────────────────────────────
async def handle_task(request):
    try:
        body     = await request.json()
        user     = body.get("user", "").strip()
        task_id  = body.get("task_id", "").strip()
        done     = bool(body.get("done", True))
        date_key = body.get("date") or today_key()

        if not user or not task_id:
            return cors(web.Response(status=400, text="Bad request"))

        data = load_json(DATA_FILE, {})
        data.setdefault(date_key, {}).setdefault(user, {})
        data[date_key][user][task_id] = done
        save_json(DATA_FILE, data)

        return cors(web.Response(status=200, text="OK"))
    except Exception as e:
        print("task error:", e)
        return cors(web.Response(status=500, text="Server error"))

# ── GET /progress?date=...&user=... — fetch a user's progress ─────────────────
async def handle_get_progress(request):
    date_key = request.rel_url.query.get("date") or today_key()
    user     = request.rel_url.query.get("user", "").strip()
    data     = load_json(DATA_FILE, {})
    progress = data.get(date_key, {}).get(user, {})
    return cors(web.json_response({"date": date_key, "user": user, "progress": progress}))

# ── GET /ping ─────────────────────────────────────────────────────────────────
async def handle_ping(request):
    return cors(web.Response(text="OK"))

# ── HTTP server ───────────────────────────────────────────────────────────────
async def start_http():
    app = web.Application()
    app.router.add_route("OPTIONS", "/{path_info:.*}", handle_options)
    app.router.add_post("/admin/tasks",  handle_set_tasks)
    app.router.add_get("/tasks",         handle_get_tasks)
    app.router.add_post("/task",         handle_task)
    app.router.add_get("/progress",      handle_get_progress)
    app.router.add_get("/ping",          handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"HTTP server running on port {port}")

# ── Bot ready ─────────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    await tree.sync()
    daily_summary.start()
    print(f"Bot ready as {client.user}")

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    await start_http()
    await client.start(DISCORD_TOKEN)

asyncio.run(main())
