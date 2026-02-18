import os
import re
import time
import json
import asyncio
import sqlite3
import datetime
from typing import Optional, Dict, Any, Tuple

import discord
from discord.ext import commands
from discord import app_commands

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# -------------------------
# CONFIGURATION
# -------------------------
PORT = int(os.environ.get("PORT", 10000))
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "CHANGE_ME")  # <-- IMPORTANT
DB_PATH = os.environ.get("DB_PATH", "leviathan.db")

START_TIME = time.time()

# -------------------------
# DATABASE (SQLite)
# -------------------------
def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def db_init():
    con = db_connect()
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS guild_config (
        guild_id INTEGER PRIMARY KEY,
        modlog_channel_id INTEGER,
        welcome_channel_id INTEGER,
        welcome_message TEXT,
        automod_enabled INTEGER DEFAULT 1,
        anti_invite INTEGER DEFAULT 1,
        anti_link INTEGER DEFAULT 0,
        spam_interval_sec REAL DEFAULT 2.0,
        spam_burst INTEGER DEFAULT 5,
        spam_timeout_min INTEGER DEFAULT 10,
        ticket_category_id INTEGER
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS infractions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        mod_id INTEGER,
        type TEXT NOT NULL,          -- warn/ban/kick/timeout/etc
        reason TEXT,
        created_at TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reaction_roles (
        guild_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        emoji TEXT NOT NULL,
        role_id INTEGER NOT NULL,
        PRIMARY KEY (guild_id, message_id, emoji)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        remind_at_ts INTEGER NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)
    con.commit()
    con.close()

def get_guild_config(guild_id: int) -> Dict[str, Any]:
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT OR IGNORE INTO guild_config(guild_id) VALUES (?)", (guild_id,))
        con.commit()
        cur.execute("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,))
        row = cur.fetchone()
    con.close()
    return dict(row)

def set_guild_config(guild_id: int, **kwargs):
    if not kwargs:
        return
    con = db_connect()
    cur = con.cursor()
    keys = []
    vals = []
    for k, v in kwargs.items():
        keys.append(f"{k} = ?")
        vals.append(v)
    vals.append(guild_id)
    cur.execute(f"UPDATE guild_config SET {', '.join(keys)} WHERE guild_id = ?", tuple(vals))
    con.commit()
    con.close()

def add_infraction(guild_id: int, user_id: int, mod_id: Optional[int], inf_type: str, reason: str):
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO infractions(guild_id,user_id,mod_id,type,reason,created_at) VALUES (?,?,?,?,?,?)",
        (guild_id, user_id, mod_id, inf_type, reason, datetime.datetime.utcnow().isoformat())
    )
    con.commit()
    con.close()

def list_infractions(guild_id: int, user_id: int, limit: int = 20):
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        "SELECT * FROM infractions WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
        (guild_id, user_id, limit)
    )
    rows = cur.fetchall()
    con.close()
    return [dict(r) for r in rows]

def clear_warns(guild_id: int, user_id: int):
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        "DELETE FROM infractions WHERE guild_id=? AND user_id=? AND type='warn'",
        (guild_id, user_id)
    )
    con.commit()
    con.close()

def rr_add(guild_id: int, message_id: int, emoji: str, role_id: int):
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO reaction_roles(guild_id,message_id,emoji,role_id) VALUES (?,?,?,?)",
        (guild_id, message_id, emoji, role_id)
    )
    con.commit()
    con.close()

def rr_remove(guild_id: int, message_id: int, emoji: str):
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        "DELETE FROM reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?",
        (guild_id, message_id, emoji)
    )
    con.commit()
    con.close()

def rr_get(guild_id: int, message_id: int, emoji: str) -> Optional[int]:
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        "SELECT role_id FROM reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?",
        (guild_id, message_id, emoji)
    )
    row = cur.fetchone()
    con.close()
    return int(row["role_id"]) if row else None

def reminder_add(user_id: int, remind_at_ts: int, content: str):
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO reminders(user_id,remind_at_ts,content,created_at) VALUES (?,?,?,?)",
        (user_id, remind_at_ts, content, datetime.datetime.utcnow().isoformat())
    )
    con.commit()
    con.close()

def reminder_due(now_ts: int, limit: int = 20):
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM reminders WHERE remind_at_ts <= ? ORDER BY remind_at_ts ASC LIMIT ?", (now_ts, limit))
    rows = cur.fetchall()
    con.close()
    return [dict(r) for r in rows]

def reminder_delete(reminder_id: int):
    con = db_connect()
    cur = con.cursor()
    cur.execute("DELETE FROM reminders WHERE id=?", (reminder_id,))
    con.commit()
    con.close()

# -------------------------
# BOT SETUP
# -------------------------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
action_logs = []

def add_log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{ts}] {msg}"
    action_logs.append(log_entry)
    if len(action_logs) > 200:
        action_logs.pop(0)
    print(log_entry)

async def send_modlog(guild: discord.Guild, text: str):
    cfg = get_guild_config(guild.id)
    cid = cfg.get("modlog_channel_id")
    if cid:
        ch = guild.get_channel(int(cid))
        if ch:
            try:
                await ch.send(text)
            except:
                pass

def is_admin():
    async def predicate(ctx: commands.Context):
        return ctx.author.guild_permissions.administrator
    return commands.check(predicate)

# -------------------------
# AUTOMOD (simple)
# -------------------------
# user_id -> list[timestamps]
spam_tracker: Dict[Tuple[int, int], list] = {}  # (guild_id, user_id) -> timestamps

INVITE_RE = re.compile(r"(discord\.gg/|discord\.com/invite/)", re.IGNORECASE)
URL_RE = re.compile(r"https?://", re.IGNORECASE)

async def automod_check(message: discord.Message):
    if not message.guild or message.author.bot:
        return
    cfg = get_guild_config(message.guild.id)
    if not cfg.get("automod_enabled", 1):
        return

    # Anti invite
    if cfg.get("anti_invite", 1) and INVITE_RE.search(message.content or ""):
        try:
            await message.delete()
        except:
            pass
        await send_modlog(message.guild, f"🚫 Anti-invite: message supprimé de {message.author.mention} dans {message.channel.mention}")
        return

    # Anti link
    if cfg.get("anti_link", 0) and URL_RE.search(message.content or ""):
        try:
            await message.delete()
        except:
            pass
        await send_modlog(message.guild, f"🔗 Anti-link: message supprimé de {message.author.mention} dans {message.channel.mention}")
        return

    # Anti spam burst
    interval = float(cfg.get("spam_interval_sec", 2.0))
    burst = int(cfg.get("spam_burst", 5))
    timeout_min = int(cfg.get("spam_timeout_min", 10))

    key = (message.guild.id, message.author.id)
    now = time.time()
    timestamps = spam_tracker.get(key, [])
    timestamps = [t for t in timestamps if now - t <= interval]
    timestamps.append(now)
    spam_tracker[key] = timestamps

    if len(timestamps) >= burst and message.author.guild_permissions.manage_messages is False:
        # punish
        try:
            duration = datetime.timedelta(minutes=timeout_min)
            await message.author.timeout(duration, reason="Automod: spam")
            add_infraction(message.guild.id, message.author.id, None, "timeout", "Automod: spam")
            await send_modlog(
                message.guild,
                f"⛔ Automod spam: {message.author.mention} timeout {timeout_min} min."
            )
        except Exception as e:
            await send_modlog(message.guild, f"⚠️ Automod spam erreur: {e}")

# -------------------------
# DASHBOARD WEB (FastAPI)
# -------------------------
app = FastAPI()

@app.get("/leviathan", response_class=HTMLResponse)
async def panel():
    if not bot.guilds:
        guild_opts = "<option>Aucun serveur (Bot Offline)</option>"
    else:
        guild_opts = "".join([f"<option value='{g.id}'>{g.name}</option>" for g in bot.guilds])

    return f"""
<!DOCTYPE html>
<html>
<head>
    <title>LEVIATHAN /// ADMIN PANEL</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ background-color: #050505; color: #00ff41; font-family: 'Courier New', monospace; padding: 20px; }}
        .header {{ border-bottom: 2px solid #00ff41; padding-bottom: 10px; margin-bottom: 20px; }}
        .card {{ background: #111; border: 1px solid #333; padding: 15px; margin-bottom: 15px; }}
        h3 {{ margin-top: 0; color: #fff; border-left: 4px solid #00ff41; padding-left: 10px; }}
        button {{ width: 100%; padding: 12px; margin: 5px 0; background: #000; color: #00ff41; border: 1px solid #00ff41; cursor: pointer; font-weight: bold; }}
        button:hover {{ background: #00ff41; color: #000; }}
        .danger-btn {{ color: #ff003c; border-color: #ff003c; }}
        .danger-btn:hover {{ background: #ff003c; color: white; }}
        input, select {{ width: 100%; padding: 10px; background: #222; border: 1px solid #555; color: white; margin-bottom: 10px; box-sizing: border-box; }}
        #console {{ height: 250px; background: black; border: 1px solid #555; overflow-y: scroll; padding: 10px; font-size: 12px; color: #ccc; }}
        .row {{ display:grid; grid-template-columns: 1fr 1fr; gap:10px; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🛡️ LEVIATHAN CONTROL</h1>
        <div>STATUS: <span style="color:#00ff41">ONLINE</span></div>
    </div>

    <div class="card">
        <h3>1. CONNEXION</h3>
        <input type="password" id="key" placeholder="MOT DE PASSE">
        <select id="guild">{guild_opts}</select>
        <button onclick="loadCfg()">📥 Charger la config</button>
    </div>

    <div class="row">
        <div class="card">
            <h3>2. SÉCURITÉ</h3>
            <button onclick="send('lockdown', true)">🔒 LOCKDOWN</button>
            <button onclick="send('lockdown', false)">🔓 UNLOCKDOWN</button>
            <button class="danger-btn" onclick="send('purge_global', 20)">🧹 PURGE GLOBAL (20)</button>
        </div>

        <div class="card">
            <h3>3. SANCTIONS</h3>
            <input type="text" id="target" placeholder="ID UTILISATEUR">
            <input type="text" id="reason" placeholder="Raison (optionnel)">
            <button onclick="send('timeout')">🤐 TIMEOUT 1H</button>
            <button class="danger-btn" onclick="send('kick')">👢 KICK</button>
            <button class="danger-btn" onclick="send('ban')">🔨 BAN</button>
        </div>
    </div>

    <div class="card">
        <h3>4. AUTOMOD / WELCOME</h3>
        <label><input type="checkbox" id="automod_enabled"> Automod activé</label><br>
        <label><input type="checkbox" id="anti_invite"> Anti-invite</label><br>
        <label><input type="checkbox" id="anti_link"> Anti-link</label><br>
        <input type="text" id="modlog_channel_id" placeholder="Modlog Channel ID">
        <input type="text" id="welcome_channel_id" placeholder="Welcome Channel ID">
        <input type="text" id="welcome_message" placeholder="Welcome message (ex: Bienvenue {user} !)">
        <button onclick="saveCfg()">💾 Sauvegarder la config</button>
    </div>

    <div class="card">
        <h3>📡 LOGS SYSTÈME</h3>
        <div id="console">En attente de commandes...</div>
    </div>

    <script>
        function log(text) {{
            const con = document.getElementById('console');
            con.innerHTML += `<div>${{text}}</div>`;
            con.scrollTop = con.scrollHeight;
        }}

        async function send(action, val=null) {{
            const key = document.getElementById('key').value;
            const guild = document.getElementById('guild').value;
            const target = document.getElementById('target').value;
            const reason = document.getElementById('reason').value || "Via Panel";
            log(`> Action: ${{action}}...`);

            try {{
                const req = await fetch('/api/run', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{ k: key, g: guild, action, val, target, reason }})
                }});
                const res = await req.json();
                if(res.error) log(`ERREUR: ${{res.error}}`);
                else log(`RÉPONSE: ${{res.details}}`);
            }} catch(e) {{
                log("Erreur de connexion au serveur.");
            }}
        }}

        async function loadCfg() {{
            const key = document.getElementById('key').value;
            const guild = document.getElementById('guild').value;
            const r = await fetch('/api/config/get', {{
                method:'POST',
                headers:{{'Content-Type':'application/json'}},
                body: JSON.stringify({{k:key, g:guild}})
            }});
            const d = await r.json();
            if(d.error) return log("ERREUR: " + d.error);

            document.getElementById('automod_enabled').checked = !!d.automod_enabled;
            document.getElementById('anti_invite').checked = !!d.anti_invite;
            document.getElementById('anti_link').checked = !!d.anti_link;
            document.getElementById('modlog_channel_id').value = d.modlog_channel_id || "";
            document.getElementById('welcome_channel_id').value = d.welcome_channel_id || "";
            document.getElementById('welcome_message').value = d.welcome_message || "";
            log("> Config chargée.");
        }}

        async function saveCfg() {{
            const key = document.getElementById('key').value;
            const guild = document.getElementById('guild').value;
            const payload = {{
                k:key, g:guild,
                automod_enabled: document.getElementById('automod_enabled').checked,
                anti_invite: document.getElementById('anti_invite').checked,
                anti_link: document.getElementById('anti_link').checked,
                modlog_channel_id: document.getElementById('modlog_channel_id').value,
                welcome_channel_id: document.getElementById('welcome_channel_id').value,
                welcome_message: document.getElementById('welcome_message').value
            }};
            const r = await fetch('/api/config/set', {{
                method:'POST',
                headers:{{'Content-Type':'application/json'}},
                body: JSON.stringify(payload)
            }});
            const d = await r.json();
            if(d.error) return log("ERREUR: " + d.error);
            log("> Config sauvegardée.");
        }}

        setInterval(async () => {{
            try {{
                const r = await fetch('/api/logs');
                const d = await r.json();
                if(d.length > 0) {{
                    document.getElementById('console').innerHTML =
                      d.slice().reverse().map(l => `<div>${{l}}</div>`).join('');
                }}
            }} catch(e) {{}}
        }}, 3000);
    </script>
</body>
</html>
"""

# -------------------------
# PANEL ACTIONS (backend)
# -------------------------
async def run_lockdown(guild_id, status: bool):
    guild = bot.get_guild(guild_id)
    if not guild:
        return "Serveur introuvable"
    role = guild.default_role
    count = 0
    for channel in guild.text_channels:
        try:
            overwrite = channel.overwrites_for(role)
            overwrite.send_messages = not status
            await channel.set_permissions(role, overwrite=overwrite)
            count += 1
        except:
            continue
    state = "VERROUILLÉ" if status else "DÉVERROUILLÉ"
    add_log(f"LOCKDOWN: Serveur {state} ({count} salons).")
    await send_modlog(guild, f"🔒 LOCKDOWN={status} via panel ({count} salons)")
    return f"Succès : {count} salons modifiés."

async def run_purge_global(guild_id, amount: int):
    guild = bot.get_guild(guild_id)
    if not guild:
        return "Serveur introuvable"
    total = 0
    for channel in guild.text_channels:
        try:
            deleted = await channel.purge(limit=amount)
            total += len(deleted)
        except:
            continue
    add_log(f"PURGE_GLOBAL: {total} messages supprimés.")
    await send_modlog(guild, f"🧹 Purge global via panel: {total} messages.")
    return f"{total} messages supprimés."

async def run_punishment(guild_id, user_id, action, reason):
    guild = bot.get_guild(guild_id)
    if not guild:
        return "Serveur introuvable"
    try:
        member = await guild.fetch_member(user_id)
    except:
        return "Membre introuvable."

    try:
        if action == "kick":
            await member.kick(reason=reason)
            add_infraction(guild_id, user_id, None, "kick", reason)
            add_log(f"KICK: {member} expulsé.")
            await send_modlog(guild, f"👢 Kick: {member.mention} | {reason}")
            return f"{member.name} a été kick."
        elif action == "ban":
            await member.ban(reason=reason)
            add_infraction(guild_id, user_id, None, "ban", reason)
            add_log(f"BAN: {member} banni.")
            await send_modlog(guild, f"🔨 Ban: {member.mention} | {reason}")
            return f"{member.name} a été ban."
        elif action == "timeout":
            duration = datetime.timedelta(hours=1)
            await member.timeout(duration, reason=reason)
            add_infraction(guild_id, user_id, None, "timeout", reason)
            add_log(f"TIMEOUT: {member} (1h).")
            await send_modlog(guild, f"🤐 Timeout 1h: {member.mention} | {reason}")
            return f"{member.name} est timeout 1h."
    except Exception as e:
        return f"Erreur permission : {str(e)}"

@app.post("/api/run")
async def api_run(request: Request):
    try:
        data = await request.json()
        if data.get("k") != ADMIN_KEY:
            return JSONResponse({"error": "MOT DE PASSE INCORRECT"}, status_code=403)

        gid = int(data["g"])
        action = data["action"]

        if action == "lockdown":
            res = await run_lockdown(gid, bool(data["val"]))
        elif action == "purge_global":
            res = await run_purge_global(gid, int(data["val"]))
        elif action in ["kick", "ban", "timeout"]:
            if not data.get("target"):
                return {"details": "Il faut un ID utilisateur !"}
            res = await run_punishment(gid, int(data["target"]), action, data.get("reason", "Via Panel"))
        else:
            res = "Commande inconnue"
        return {"status": "ok", "details": res}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/logs")
async def get_logs():
    return action_logs

@app.post("/api/config/get")
async def api_cfg_get(request: Request):
    data = await request.json()
    if data.get("k") != ADMIN_KEY:
        return JSONResponse({"error": "MOT DE PASSE INCORRECT"}, status_code=403)
    gid = int(data["g"])
    cfg = get_guild_config(gid)
    return cfg

@app.post("/api/config/set")
async def api_cfg_set(request: Request):
    data = await request.json()
    if data.get("k") != ADMIN_KEY:
        return JSONResponse({"error": "MOT DE PASSE INCORRECT"}, status_code=403)
    gid = int(data["g"])

    def as_int_or_none(v):
        v = (v or "").strip()
        return int(v) if v.isdigit() else None

    set_guild_config(
        gid,
        automod_enabled=1 if data.get("automod_enabled") else 0,
        anti_invite=1 if data.get("anti_invite") else 0,
        anti_link=1 if data.get("anti_link") else 0,
        modlog_channel_id=as_int_or_none(data.get("modlog_channel_id")),
        welcome_channel_id=as_int_or_none(data.get("welcome_channel_id")),
        welcome_message=(data.get("welcome_message") or "").strip() or None
    )
    return {"status": "ok"}

@app.post("/api/infractions")
async def api_infractions(request: Request):
    data = await request.json()
    if data.get("k") != ADMIN_KEY:
        return JSONResponse({"error": "MOT DE PASSE INCORRECT"}, status_code=403)
    gid = int(data["g"])
    uid = int(data["u"])
    return list_infractions(gid, uid, limit=int(data.get("limit", 20)))

# -------------------------
# BOT EVENTS
# -------------------------
@bot.event
async def on_ready():
    add_log(f"Bot connecté en tant que {bot.user} | guilds={len(bot.guilds)}")
    try:
        await bot.tree.sync()
        add_log("Slash commands sync ✅")
    except Exception as e:
        add_log(f"Slash sync error: {e}")
    if not reminder_loop.is_running():
        reminder_loop.start()

@bot.event
async def on_message(message: discord.Message):
    await automod_check(message)
    await bot.process_commands(message)

@bot.event
async def on_member_join(member: discord.Member):
    cfg = get_guild_config(member.guild.id)
    ch_id = cfg.get("welcome_channel_id")
    if ch_id:
        ch = member.guild.get_channel(int(ch_id))
        if ch:
            msg = cfg.get("welcome_message") or "Bienvenue {user} !"
            msg = msg.replace("{user}", member.mention).replace("{server}", member.guild.name)
            try:
                await ch.send(msg)
            except:
                pass

@bot.event
async def on_member_remove(member: discord.Member):
    await send_modlog(member.guild, f"👋 Départ: {member} ({member.id})")

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None or payload.member is None or payload.member.bot:
        return
    role_id = rr_get(payload.guild_id, payload.message_id, str(payload.emoji))
    if not role_id:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    role = guild.get_role(role_id)
    if role:
        try:
            await payload.member.add_roles(role, reason="Reaction role")
        except:
            pass

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None:
        return
    role_id = rr_get(payload.guild_id, payload.message_id, str(payload.emoji))
    if not role_id:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    member = guild.get_member(payload.user_id)
    if not member or member.bot:
        return
    role = guild.get_role(role_id)
    if role:
        try:
            await member.remove_roles(role, reason="Reaction role")
        except:
            pass

# -------------------------
# REMINDER LOOP
# -------------------------
from discord.ext import tasks

@tasks.loop(seconds=10)
async def reminder_loop():
    now_ts = int(time.time())
    due = reminder_due(now_ts, limit=20)
    for r in due:
        user = bot.get_user(int(r["user_id"]))
        if user:
            try:
                when = datetime.datetime.fromtimestamp(int(r["remind_at_ts"]))
                await user.send(f"⏰ Rappel ({when}): {r['content']}")
            except:
                pass
        reminder_delete(int(r["id"]))

# -------------------------
# HELPERS
# -------------------------
def parse_duration_to_seconds(s: str) -> Optional[int]:
    """
    Ex: 10m, 2h, 3d
    """
    s = (s or "").strip().lower()
    m = re.fullmatch(r"(\d+)\s*([smhd])", s)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return n * mult

# -------------------------
# PREFIX COMMANDS
# -------------------------
@bot.command()
async def ping(ctx):
    await ctx.send(f"🏓 Pong: {round(bot.latency*1000)}ms")

@bot.command()
async def uptime(ctx):
    up = int(time.time() - START_TIME)
    await ctx.send(f"⏱️ Uptime: {up}s")

@bot.command()
async def serverinfo(ctx):
    g = ctx.guild
    await ctx.send(f"🏠 **{g.name}** | membres={g.member_count} | salons={len(g.channels)} | owner={g.owner}")

@bot.command()
async def userinfo(ctx, member: Optional[discord.Member] = None):
    m = member or ctx.author
    await ctx.send(f"👤 **{m}** | id={m.id} | créé={m.created_at.date()} | rejoint={m.joined_at.date() if m.joined_at else 'n/a'}")

@bot.command()
@is_admin()
async def say(ctx, *, text: str):
    await ctx.message.delete()
    await ctx.send(text)

@bot.command()
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int = 20):
    amount = max(1, min(amount, 200))
    deleted = await ctx.channel.purge(limit=amount + 1)
    await send_modlog(ctx.guild, f"🧹 Purge: {len(deleted)-1} messages par {ctx.author.mention} dans {ctx.channel.mention}")

@bot.command()
@commands.has_permissions(manage_channels=True)
async def lock(ctx):
    role = ctx.guild.default_role
    overwrite = ctx.channel.overwrites_for(role)
    overwrite.send_messages = False
    await ctx.channel.set_permissions(role, overwrite=overwrite)
    await send_modlog(ctx.guild, f"🔒 Lock: {ctx.channel.mention} par {ctx.author.mention}")

@bot.command()
@commands.has_permissions(manage_channels=True)
async def unlock(ctx):
    role = ctx.guild.default_role
    overwrite = ctx.channel.overwrites_for(role)
    overwrite.send_messages = None
    await ctx.channel.set_permissions(role, overwrite=overwrite)
    await send_modlog(ctx.guild, f"🔓 Unlock: {ctx.channel.mention} par {ctx.author.mention}")

@bot.command()
@commands.has_permissions(manage_channels=True)
async def slowmode(ctx, seconds: int = 0):
    seconds = max(0, min(seconds, 21600))
    await ctx.channel.edit(slowmode_delay=seconds)
    await send_modlog(ctx.guild, f"🐢 Slowmode: {seconds}s dans {ctx.channel.mention} par {ctx.author.mention}")

@bot.command()
@commands.has_permissions(manage_nicknames=True)
async def nick(ctx, member: discord.Member, *, nickname: str):
    await member.edit(nick=nickname)
    await send_modlog(ctx.guild, f"✏️ Nick: {member.mention} -> `{nickname}` par {ctx.author.mention}")

@bot.command()
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason: str = "Aucune raison"):
    await member.kick(reason=reason)
    add_infraction(ctx.guild.id, member.id, ctx.author.id, "kick", reason)
    await send_modlog(ctx.guild, f"👢 Kick: {member.mention} | {reason} | par {ctx.author.mention}")

@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason: str = "Aucune raison"):
    await member.ban(reason=reason)
    add_infraction(ctx.guild.id, member.id, ctx.author.id, "ban", reason)
    await send_modlog(ctx.guild, f"🔨 Ban: {member.mention} | {reason} | par {ctx.author.mention}")

@bot.command()
@commands.has_permissions(ban_members=True)
async def unban(ctx, user_id: int):
    user = await bot.fetch_user(user_id)
    await ctx.guild.unban(user, reason=f"Unban par {ctx.author}")
    await send_modlog(ctx.guild, f"✅ Unban: {user} par {ctx.author.mention}")

@bot.command()
@commands.has_permissions(moderate_members=True)
async def mute(ctx, member: discord.Member, duration: str = "10m", *, reason: str = "Aucune raison"):
    sec = parse_duration_to_seconds(duration) or 600
    td = datetime.timedelta(seconds=sec)
    await member.timeout(td, reason=reason)
    add_infraction(ctx.guild.id, member.id, ctx.author.id, "timeout", f"{duration} | {reason}")
    await send_modlog(ctx.guild, f"🤐 Timeout: {member.mention} {duration} | {reason} | par {ctx.author.mention}")

@bot.command()
@commands.has_permissions(moderate_members=True)
async def unmute(ctx, member: discord.Member, *, reason: str = "Aucune raison"):
    await member.timeout(None, reason=reason)
    add_infraction(ctx.guild.id, member.id, ctx.author.id, "untimeout", reason)
    await send_modlog(ctx.guild, f"🔈 Un-timeout: {member.mention} | {reason} | par {ctx.author.mention}")

@bot.command()
@commands.has_permissions(moderate_members=True)
async def warn(ctx, member: discord.Member, *, reason: str = "Aucune raison"):
    add_infraction(ctx.guild.id, member.id, ctx.author.id, "warn", reason)
    await send_modlog(ctx.guild, f"⚠️ Warn: {member.mention} | {reason} | par {ctx.author.mention}")
    await ctx.send(f"⚠️ {member.mention} averti. ({reason})")

@bot.command()
@commands.has_permissions(moderate_members=True)
async def infractions(ctx, member: discord.Member):
    rows = list_infractions(ctx.guild.id, member.id, limit=15)
    if not rows:
        return await ctx.send("Aucune infraction.")
    lines = []
    for r in rows:
        lines.append(f"#{r['id']} • {r['type']} • {r['created_at'][:19].replace('T',' ')} • {r.get('reason') or ''}")
    await ctx.send("```" + "\n".join(lines) + "```")

@bot.command()
@commands.has_permissions(moderate_members=True)
async def clearwarns(ctx, member: discord.Member):
    clear_warns(ctx.guild.id, member.id)
    await send_modlog(ctx.guild, f"🧽 Clear warns: {member.mention} par {ctx.author.mention}")
    await ctx.send("✅ Warns supprimés.")

@bot.command()
async def poll(ctx, question: str, *, options: str):
    opts = [o.strip() for o in options.split("|") if o.strip()]
    if len(opts) < 2 or len(opts) > 10:
        return await ctx.send("Utilise: `!poll \"Question\" option1 | option2 | option3` (2 à 10 options)")
    emojis = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    desc = "\n".join([f"{emojis[i]} {opts[i]}" for i in range(len(opts))])
    embed = discord.Embed(title=question, description=desc)
    msg = await ctx.send(embed=embed)
    for i in range(len(opts)):
        await msg.add_reaction(emojis[i])

@bot.command()
async def remind(ctx, duration: str, *, content: str):
    sec = parse_duration_to_seconds(duration)
    if not sec:
        return await ctx.send("Format durée: `10m`, `2h`, `3d`")
    remind_at = int(time.time()) + sec
    reminder_add(ctx.author.id, remind_at, content)
    await ctx.send(f"⏰ OK. Je te rappellerai dans {duration}.")

# Tickets
@bot.command()
async def ticket(ctx, *, subject: str = "Support"):
    cfg = get_guild_config(ctx.guild.id)
    category = None
    if cfg.get("ticket_category_id"):
        category = ctx.guild.get_channel(int(cfg["ticket_category_id"]))

    overwrites = {
        ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
        ctx.author: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        ctx.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
    }
    name = f"ticket-{ctx.author.name}".lower().replace(" ", "-")[:90]
    channel = await ctx.guild.create_text_channel(name=name, overwrites=overwrites, category=category)
    await channel.send(f"🎫 Ticket créé pour {ctx.author.mention}\nSujet: **{subject}**\nUtilise `!close` pour fermer.")
    await ctx.send(f"✅ Ticket: {channel.mention}")
    await send_modlog(ctx.guild, f"🎫 Ticket: {channel.mention} créé par {ctx.author.mention}")

@bot.command()
async def close(ctx):
    if not ctx.channel.name.startswith("ticket-"):
        return await ctx.send("Ce salon n’est pas un ticket.")
    await send_modlog(ctx.guild, f"🗑️ Ticket fermé: {ctx.channel.mention} par {ctx.author.mention}")
    await ctx.send("Fermeture du ticket dans 3s…")
    await asyncio.sleep(3)
    await ctx.channel.delete()

# Reaction roles
@bot.command()
@commands.has_permissions(manage_roles=True)
async def rr(ctx, sub: str, message_id: int, emoji: str, role: Optional[discord.Role] = None):
    sub = sub.lower()
    if sub == "add":
        if role is None:
            return await ctx.send("Usage: `!rr add <message_id> <emoji> <@role>`")
        rr_add(ctx.guild.id, message_id, emoji, role.id)
        await ctx.send("✅ Reaction role ajouté.")
    elif sub == "remove":
        rr_remove(ctx.guild.id, message_id, emoji)
        await ctx.send("✅ Reaction role supprimé.")
    else:
        await ctx.send("Sous-commandes: add/remove")

# -------------------------
# SLASH COMMANDS
# -------------------------
@bot.tree.command(name="ping", description="Voir la latence du bot")
async def slash_ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong: {round(bot.latency*1000)}ms", ephemeral=True)

@bot.tree.command(name="serverinfo", description="Infos du serveur")
async def slash_serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    if not g:
        return await interaction.response.send_message("Pas de serveur.", ephemeral=True)
    await interaction.response.send_message(
        f"🏠 **{g.name}** | membres={g.member_count} | salons={len(g.channels)} | owner={g.owner}",
        ephemeral=True
    )

@bot.tree.command(name="userinfo", description="Infos utilisateur")
@app_commands.describe(user="Utilisateur (optionnel)")
async def slash_userinfo(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    m = user or interaction.user
    await interaction.response.send_message(
        f"👤 **{m}** | id={m.id} | créé={m.created_at.date()}",
        ephemeral=True
    )

@bot.tree.command(name="warn", description="Avertir un membre")
@app_commands.checks.has_permissions(moderate_members=True)
async def slash_warn(interaction: discord.Interaction, user: discord.Member, reason: str = "Aucune raison"):
    add_infraction(interaction.guild_id, user.id, interaction.user.id, "warn", reason)
    await send_modlog(interaction.guild, f"⚠️ Warn: {user.mention} | {reason} | par {interaction.user.mention}")
    await interaction.response.send_message("✅ Warn enregistré.", ephemeral=True)

@bot.tree.command(name="infractions", description="Voir les infractions d'un membre")
@app_commands.checks.has_permissions(moderate_members=True)
async def slash_infractions(interaction: discord.Interaction, user: discord.Member):
    rows = list_infractions(interaction.guild_id, user.id, limit=10)
    if not rows:
        return await interaction.response.send_message("Aucune infraction.", ephemeral=True)
    txt = "\n".join([f"#{r['id']} • {r['type']} • {r['created_at'][:19].replace('T',' ')} • {r.get('reason') or ''}" for r in rows])
    await interaction.response.send_message("```" + txt + "```", ephemeral=True)

@bot.tree.command(name="ticket", description="Créer un ticket support")
async def slash_ticket(interaction: discord.Interaction, subject: str = "Support"):
    # On réutilise la logique de !ticket
    ctx = await bot.get_context(await interaction.original_response())  # fallback (rare)
    # plus simple: appeler la fonction via une création directe
    cfg = get_guild_config(interaction.guild_id)
    category = None
    if cfg.get("ticket_category_id"):
        category = interaction.guild.get_channel(int(cfg["ticket_category_id"]))

    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
        interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        interaction.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
    }
    name = f"ticket-{interaction.user.name}".lower().replace(" ", "-")[:90]
    channel = await interaction.guild.create_text_channel(name=name, overwrites=overwrites, category=category)
    await channel.send(f"🎫 Ticket créé pour {interaction.user.mention}\nSujet: **{subject}**\nUtilise `!close` pour fermer.")
    await interaction.response.send_message(f"✅ Ticket: {channel.mention}", ephemeral=True)
    await send_modlog(interaction.guild, f"🎫 Ticket: {channel.mention} créé par {interaction.user.mention}")

# -------------------------
# MAIN
# -------------------------
async def main():
    db_init()
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT)
    server = uvicorn.Server(config)
    await asyncio.gather(bot.start(DISCORD_TOKEN), server.serve())

if __name__ == "__main__":
    asyncio.run(main())
