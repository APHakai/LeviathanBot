import os
import re
import time
import json
import math
import asyncio
import random
import sqlite3
import datetime
import io
from typing import Optional, Dict, Any, Tuple, List

import discord
from discord.ext import commands, tasks
from discord import app_commands

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# =========================================================
# ENV / CONFIG
# =========================================================
PORT = int(os.environ.get("PORT", 10000))
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "CHANGE_ME")  # change via Render env
DB_PATH = os.environ.get("DB_PATH", "leviathan.db")

START_TIME = time.time()

# =========================================================
# DATABASE
# =========================================================
def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def db_init():
    con = db_connect()
    cur = con.cursor()

    # guild config
    cur.execute("""
    CREATE TABLE IF NOT EXISTS guild_config (
        guild_id INTEGER PRIMARY KEY,
        modlog_channel_id INTEGER,
        welcome_channel_id INTEGER,
        welcome_message TEXT,
        goodbye_channel_id INTEGER,
        goodbye_message TEXT,

        automod_enabled INTEGER DEFAULT 1,
        anti_invite INTEGER DEFAULT 1,
        anti_link INTEGER DEFAULT 0,
        anti_caps INTEGER DEFAULT 0,
        caps_threshold INTEGER DEFAULT 70,

        spam_interval_sec REAL DEFAULT 2.0,
        spam_burst INTEGER DEFAULT 5,
        spam_timeout_min INTEGER DEFAULT 10,

        ticket_category_id INTEGER,
        suggestion_channel_id INTEGER,

        leveling_enabled INTEGER DEFAULT 1,
        economy_enabled INTEGER DEFAULT 1
    )
    """)

    # infractions
    cur.execute("""
    CREATE TABLE IF NOT EXISTS infractions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        mod_id INTEGER,
        type TEXT NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL
    )
    """)

    # reaction roles
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reaction_roles (
        guild_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        emoji TEXT NOT NULL,
        role_id INTEGER NOT NULL,
        PRIMARY KEY (guild_id, message_id, emoji)
    )
    """)

    # reminders
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        remind_at_ts INTEGER NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    # leveling / xp
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_xp (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        xp INTEGER NOT NULL DEFAULT 0,
        level INTEGER NOT NULL DEFAULT 0,
        last_xp_ts INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    )
    """)

    # economy
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_econ (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        balance INTEGER NOT NULL DEFAULT 0,
        last_daily_ts INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    )
    """)

    # shop items
    cur.execute("""
    CREATE TABLE IF NOT EXISTS shop_items (
        guild_id INTEGER NOT NULL,
        item_key TEXT NOT NULL,
        name TEXT NOT NULL,
        price INTEGER NOT NULL,
        description TEXT,
        PRIMARY KEY (guild_id, item_key)
    )
    """)

    # giveaways
    cur.execute("""
    CREATE TABLE IF NOT EXISTS giveaways (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        end_ts INTEGER NOT NULL,
        winners INTEGER NOT NULL,
        prize TEXT NOT NULL,
        emoji TEXT NOT NULL DEFAULT '🎉',
        ended INTEGER NOT NULL DEFAULT 0
    )
    """)

    con.commit()
    con.close()

# --------- helpers: config ----------
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
        keys.append(f"{k}=?")
        vals.append(v)
    vals.append(guild_id)
    cur.execute(f"UPDATE guild_config SET {', '.join(keys)} WHERE guild_id=?", tuple(vals))
    con.commit()
    con.close()

# --------- helpers: infractions ----------
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
    cur.execute("DELETE FROM infractions WHERE guild_id=? AND user_id=? AND type='warn'", (guild_id, user_id))
    con.commit()
    con.close()

# --------- helpers: reaction roles ----------
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
    cur.execute("DELETE FROM reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?", (guild_id, message_id, emoji))
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

# --------- helpers: reminders ----------
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
    cur.execute("SELECT * FROM reminders WHERE remind_at_ts<=? ORDER BY remind_at_ts ASC LIMIT ?", (now_ts, limit))
    rows = cur.fetchall()
    con.close()
    return [dict(r) for r in rows]

def reminder_delete(reminder_id: int):
    con = db_connect()
    cur = con.cursor()
    cur.execute("DELETE FROM reminders WHERE id=?", (reminder_id,))
    con.commit()
    con.close()

# --------- helpers: leveling ----------
def xp_get(guild_id: int, user_id: int) -> Dict[str, int]:
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM user_xp WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT OR IGNORE INTO user_xp(guild_id,user_id) VALUES (?,?)", (guild_id, user_id))
        con.commit()
        cur.execute("SELECT * FROM user_xp WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        row = cur.fetchone()
    con.close()
    return dict(row)

def xp_set(guild_id: int, user_id: int, xp: int, level: int, last_xp_ts: int):
    con = db_connect()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO user_xp(guild_id,user_id,xp,level,last_xp_ts)
        VALUES (?,?,?,?,?)
        ON CONFLICT(guild_id,user_id) DO UPDATE SET xp=excluded.xp, level=excluded.level, last_xp_ts=excluded.last_xp_ts
    """, (guild_id, user_id, xp, level, last_xp_ts))
    con.commit()
    con.close()

def xp_level_from_xp(xp: int) -> int:
    # simple curve
    # level ~= sqrt(xp/100)
    return int(math.sqrt(max(0, xp) / 100))

def xp_needed_for_level(level: int) -> int:
    return int((level ** 2) * 100)

def xp_leaderboard(guild_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    con = db_connect()
    cur = con.cursor()
    cur.execute("""
        SELECT user_id, xp, level FROM user_xp
        WHERE guild_id=?
        ORDER BY xp DESC
        LIMIT ?
    """, (guild_id, limit))
    rows = cur.fetchall()
    con.close()
    return [dict(r) for r in rows]

# --------- helpers: economy ----------
def econ_get(guild_id: int, user_id: int) -> Dict[str, int]:
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM user_econ WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT OR IGNORE INTO user_econ(guild_id,user_id) VALUES (?,?)", (guild_id, user_id))
        con.commit()
        cur.execute("SELECT * FROM user_econ WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        row = cur.fetchone()
    con.close()
    return dict(row)

def econ_set(guild_id: int, user_id: int, balance: int, last_daily_ts: int):
    con = db_connect()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO user_econ(guild_id,user_id,balance,last_daily_ts)
        VALUES (?,?,?,?)
        ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=excluded.balance, last_daily_ts=excluded.last_daily_ts
    """, (guild_id, user_id, balance, last_daily_ts))
    con.commit()
    con.close()

def shop_seed_if_empty(guild_id: int):
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM shop_items WHERE guild_id=?", (guild_id,))
    c = int(cur.fetchone()["c"])
    if c == 0:
        items = [
            ("vip_week", "VIP 7 jours", 500, "Accès VIP pendant 7 jours (symbolique)"),
            ("color_role", "Rôle couleur", 250, "Un rôle couleur custom (staff)"),
            ("shoutout", "Shoutout", 150, "Annonce / message spécial (staff)"),
        ]
        for key, name, price, desc in items:
            cur.execute("""
                INSERT OR IGNORE INTO shop_items(guild_id,item_key,name,price,description)
                VALUES (?,?,?,?,?)
            """, (guild_id, key, name, price, desc))
        con.commit()
    con.close()

def shop_list(guild_id: int) -> List[Dict[str, Any]]:
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM shop_items WHERE guild_id=? ORDER BY price ASC", (guild_id,))
    rows = cur.fetchall()
    con.close()
    return [dict(r) for r in rows]

# --------- helpers: giveaways ----------
def giveaway_create(guild_id: int, channel_id: int, message_id: int, end_ts: int, winners: int, prize: str, emoji: str = "🎉"):
    con = db_connect()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO giveaways(guild_id,channel_id,message_id,end_ts,winners,prize,emoji,ended)
        VALUES (?,?,?,?,?,?,?,0)
    """, (guild_id, channel_id, message_id, end_ts, winners, prize, emoji))
    con.commit()
    con.close()

def giveaway_due(now_ts: int, limit: int = 10) -> List[Dict[str, Any]]:
    con = db_connect()
    cur = con.cursor()
    cur.execute("""
        SELECT * FROM giveaways
        WHERE ended=0 AND end_ts<=?
        ORDER BY end_ts ASC
        LIMIT ?
    """, (now_ts, limit))
    rows = cur.fetchall()
    con.close()
    return [dict(r) for r in rows]

def giveaway_mark_ended(giveaway_id: int):
    con = db_connect()
    cur = con.cursor()
    cur.execute("UPDATE giveaways SET ended=1 WHERE id=?", (giveaway_id,))
    con.commit()
    con.close()

# =========================================================
# BOT SETUP
# =========================================================
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

action_logs: List[str] = []

def add_log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    action_logs.append(line)
    if len(action_logs) > 400:
        action_logs.pop(0)
    print(line, flush=True)

async def send_modlog(guild: discord.Guild, text: str):
    try:
        cfg = get_guild_config(guild.id)
        cid = cfg.get("modlog_channel_id")
        if not cid:
            return
        ch = guild.get_channel(int(cid))
        if ch:
            await ch.send(text)
    except:
        pass

def parse_duration_to_seconds(s: str) -> Optional[int]:
    """Parse a human duration string into seconds.

    Units:
      s seconds, m|min minutes, h hours, d days, w weeks (7d),
      mo months (30d), y years (365d).

    Examples: 10m, 2h, 1d, 1w, 1mo, 1y, 1h30m, 2w3d, 1 d 6 h 10 m
    """
    s = (s or "").strip().lower()
    if not s:
        return None
    s = re.sub(r"\s+", "", s)

    token_re = re.compile(r"(\d+)(mo|min|[smhdwy])")
    pos = 0
    total = 0
    for m in token_re.finditer(s):
        if m.start() != pos:
            return None
        n = int(m.group(1))
        unit = m.group(2)
        mult = {
            "s": 1,
            "m": 60,
            "min": 60,
            "h": 3600,
            "d": 86400,
            "w": 7 * 86400,
            "mo": 30 * 86400,
            "y": 365 * 86400,
        }[unit]
        total += n * mult
        pos = m.end()

    if pos != len(s) or total <= 0:
        return None
    return total


# =========================================================
# AUTOMOD
# =========================================================
spam_tracker: Dict[Tuple[int, int], list] = {}
INVITE_RE = re.compile(r"(discord\.gg/|discord\.com/invite/)", re.IGNORECASE)
URL_RE = re.compile(r"https?://", re.IGNORECASE)

def caps_ratio(text: str) -> int:
    if not text:
        return 0
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0
    caps = [c for c in letters if c.isupper()]
    return int((len(caps) / len(letters)) * 100)

async def automod_check(message: discord.Message):
    if not message.guild or message.author.bot:
        return

    cfg = get_guild_config(message.guild.id)
    if not cfg.get("automod_enabled", 1):
        return

    content = message.content or ""

    # anti invite
    if cfg.get("anti_invite", 1) and INVITE_RE.search(content):
        try:
            await message.delete()
        except:
            pass
        await send_modlog(message.guild, f"🚫 Anti-invite: supprimé ({message.author.mention}) dans {message.channel.mention}")
        return

    # anti link
    if cfg.get("anti_link", 0) and URL_RE.search(content):
        try:
            await message.delete()
        except:
            pass
        await send_modlog(message.guild, f"🔗 Anti-link: supprimé ({message.author.mention}) dans {message.channel.mention}")
        return

    # anti caps
    if cfg.get("anti_caps", 0):
        ratio = caps_ratio(content)
        thr = int(cfg.get("caps_threshold", 70))
        if ratio >= thr and len(content) >= 10 and not message.author.guild_permissions.manage_messages:
            try:
                await message.delete()
            except:
                pass
            await send_modlog(message.guild, f"🔠 Anti-caps: supprimé ({ratio}% caps) {message.author.mention} dans {message.channel.mention}")
            return

    # anti spam burst
    interval = float(cfg.get("spam_interval_sec", 2.0))
    burst = int(cfg.get("spam_burst", 5))
    timeout_min = int(cfg.get("spam_timeout_min", 10))

    key = (message.guild.id, message.author.id)
    now = time.time()
    ts = spam_tracker.get(key, [])
    ts = [t for t in ts if now - t <= interval]
    ts.append(now)
    spam_tracker[key] = ts

    if len(ts) >= burst and not message.author.guild_permissions.manage_messages:
        try:
            duration = datetime.timedelta(minutes=timeout_min)
            await message.author.timeout(duration, reason="Automod: spam")
            add_infraction(message.guild.id, message.author.id, None, "timeout", "Automod: spam")
            await send_modlog(message.guild, f"⛔ Automod spam: {message.author.mention} timeout {timeout_min} min.")
        except Exception as e:
            await send_modlog(message.guild, f"⚠️ Automod spam erreur: {e}")

# =========================================================
# LEVELING (XP on message)
# =========================================================
async def leveling_on_message(message: discord.Message):
    if not message.guild or message.author.bot:
        return
    cfg = get_guild_config(message.guild.id)
    if not cfg.get("leveling_enabled", 1):
        return

    # ignore very short messages
    if len((message.content or "").strip()) < 3:
        return

    row = xp_get(message.guild.id, message.author.id)
    now = int(time.time())
    cooldown = 30  # seconds
    if now - int(row.get("last_xp_ts", 0)) < cooldown:
        return

    gain = random.randint(10, 20)
    new_xp = int(row["xp"]) + gain
    new_level = xp_level_from_xp(new_xp)
    old_level = int(row["level"])

    xp_set(message.guild.id, message.author.id, new_xp, new_level, now)

    if new_level > old_level:
        try:
            await message.channel.send(f"✨ {message.author.mention} passe niveau **{new_level}** ! (+{gain} XP)")
        except:
            pass

# =========================================================
# FASTAPI PANEL (NO f-strings => no brace bugs)
# =========================================================
app = FastAPI()

PANEL_HTML = """

<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Leviathan Panel</title>
  <style>
    :root{
      --bg:#070A0F;
      --panel:#0D1220;
      --panel2:#101A2E;
      --border:#1F2A44;
      --text:#EAF0FF;
      --muted:#9FB0D0;
      --green:#35FF9B;
      --red:#FF3B5C;
      --blue:#5AA7FF;
      --yellow:#FFD166;
      --shadow: 0 18px 40px rgba(0,0,0,.35);
      --radius: 18px;
    }
    *{box-sizing:border-box}
    body{
      margin:0; font-family: ui-sans-serif,system-ui,Segoe UI,Roboto,Arial;
      background: radial-gradient(1200px 700px at 20% -10%, rgba(90,167,255,.25), transparent 55%),
                  radial-gradient(900px 600px at 85% 0%, rgba(53,255,155,.18), transparent 60%),
                  radial-gradient(900px 700px at 40% 110%, rgba(255,59,92,.12), transparent 60%),
                  var(--bg);
      color:var(--text);
    }
    .wrap{
      display:grid; grid-template-columns: 280px 1fr;
      min-height:100vh;
    }
    .side{
      border-right:1px solid var(--border);
      padding:22px;
      background: linear-gradient(180deg, rgba(13,18,32,.92), rgba(7,10,15,.92));
      position: sticky; top:0; height:100vh;
    }
    .brand{
      display:flex; gap:12px; align-items:center; margin-bottom:16px;
    }
    .logo{
      width:40px; height:40px; border-radius:14px;
      background: linear-gradient(135deg, rgba(53,255,155,.35), rgba(90,167,255,.35));
      border:1px solid rgba(255,255,255,.08);
      box-shadow: var(--shadow);
    }
    .brand h1{
      font-size:16px; margin:0; letter-spacing:.5px;
    }
    .pill{
      display:inline-flex; gap:8px; align-items:center;
      padding:7px 10px; border-radius:999px;
      background: rgba(255,255,255,.06);
      border:1px solid rgba(255,255,255,.08);
      color:var(--muted);
      font-size:12px;
    }
    .nav{
      margin-top:18px; display:flex; flex-direction:column; gap:10px;
    }
    .nav button{
      text-align:left;
      padding:12px 12px;
      border-radius: 14px;
      background: rgba(255,255,255,.04);
      border: 1px solid rgba(255,255,255,.06);
      color: var(--text);
      cursor:pointer;
      transition:.15s;
    }
    .nav button:hover{ transform: translateY(-1px); background: rgba(255,255,255,.07); }
    .nav button.active{
      background: linear-gradient(135deg, rgba(90,167,255,.18), rgba(53,255,155,.14));
      border-color: rgba(90,167,255,.25);
      box-shadow: 0 10px 22px rgba(0,0,0,.25);
    }

    .main{
      padding: 26px;
    }
    .topbar{
      display:flex; gap:14px; align-items:center; justify-content:space-between;
      margin-bottom:18px;
    }
    .topbar .left{
      display:flex; gap:12px; align-items:center;
    }
    .card{
      background: linear-gradient(180deg, rgba(13,18,32,.92), rgba(16,26,46,.85));
      border:1px solid rgba(255,255,255,.07);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 16px;
    }
    .grid{
      display:grid; grid-template-columns: 1fr 1fr;
      gap: 14px;
    }
    .grid3{
      display:grid; grid-template-columns: 1fr 1fr 1fr;
      gap: 14px;
    }
    .title{
      font-size:14px; color:var(--muted); margin:0 0 10px 0;
      letter-spacing:.35px;
    }
    label{ font-size:12px; color:var(--muted); display:block; margin:8px 0 6px; }
    input, select, textarea{
      width:100%;
      background: rgba(255,255,255,.04);
      border:1px solid rgba(255,255,255,.08);
      color:var(--text);
      padding: 11px 12px;
      border-radius: 14px;
      outline:none;
    }
    textarea{ min-height: 110px; resize: vertical; }
    .btn{
      padding:11px 12px; border-radius: 14px;
      border:1px solid rgba(255,255,255,.10);
      cursor:pointer;
      background: rgba(255,255,255,.06);
      color:var(--text);
      font-weight:600;
      transition:.15s;
    }
    .btn:hover{ transform: translateY(-1px); background: rgba(255,255,255,.10); }
    .btn.primary{
      background: linear-gradient(135deg, rgba(90,167,255,.30), rgba(53,255,155,.22));
      border-color: rgba(90,167,255,.25);
    }
    .btn.danger{
      background: rgba(255,59,92,.12);
      border-color: rgba(255,59,92,.25);
      color: #ffdbe2;
    }
    .row{ display:flex; gap:10px; flex-wrap:wrap; }
    .row > *{ flex:1; min-width: 160px; }
    .hint{ color:var(--muted); font-size:12px; margin-top:8px; line-height:1.35; }
    .ok{ color: var(--green); }
    .bad{ color: var(--red); }
    .tab{ display:none; }
    .tab.active{ display:block; }

    .console{
      height: 280px;
      overflow:auto;
      background: rgba(0,0,0,.35);
      border:1px solid rgba(255,255,255,.08);
      border-radius: 14px;
      padding: 12px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
      color: #cfe0ff;
    }

    .preview{
      border:1px solid rgba(255,255,255,.08);
      border-radius: 16px;
      padding: 14px;
      background: rgba(0,0,0,.20);
    }
    .preview .p-title{ font-weight:700; margin:0 0 6px 0; }
    .preview .p-desc{ margin:0 0 10px 0; color:#dbe6ff; opacity:.9; white-space: pre-wrap; }
    .field{
      border-top:1px solid rgba(255,255,255,.08);
      padding-top:10px; margin-top:10px;
      display:grid; grid-template-columns: 1fr 1fr;
      gap:10px;
    }
    .field .k{ font-weight:700; }
    @media(max-width: 980px){
      .wrap{ grid-template-columns: 1fr; }
      .side{ position:relative; height:auto; }
      .grid, .grid3{ grid-template-columns: 1fr; }
    }
  
    /* Dropdowns (fix lisibilité) */
    select{
      background-color:#111827;
      color:#F9FAFB;
      border:1px solid var(--border);
      padding:10px 12px;
      border-radius:12px;
      outline:none;
    }
    select:focus{
      border-color:var(--blue);
      box-shadow:0 0 0 2px rgba(66,153,225,.25);
    }
    option{ background-color:#111827; color:#F9FAFB; }

  </style>
</head>
<body>
<div class="wrap">
  <aside class="side">
    <div class="brand">
      <div class="logo"></div>
      <div>
        <h1>LEVIATHAN PANEL</h1>
        <div class="pill"><span id="statusDot">●</span> <span id="statusText">Status: panel online</span></div>
      </div>
    </div>

    <div class="nav">
      <button class="active" data-tab="tab-dashboard">Dashboard</button>
      <button data-tab="tab-config">Config</button>
      <button data-tab="tab-moderation">Modération</button>
      <button data-tab="tab-embed">Embed Builder</button>
      <button data-tab="tab-giveaway">Giveaways</button>
      <button data-tab="tab-tools">Outils</button>
      <button data-tab="tab-logs">Logs</button>
    </div>

    <div style="margin-top:14px" class="hint">
      <b>Connexion</b><br/>
      Le mot de passe = <code>ADMIN_KEY</code> (Render env).<br/>
      Le bot peut être offline, le panel reste accessible.
    </div>
  </aside>

  <main class="main">
    <div class="topbar">
      <div class="left">
        <div class="card" style="display:flex; gap:10px; align-items:center;">
          <div style="min-width:250px">
            <label>Mot de passe</label>
            <input type="password" id="key" placeholder="ADMIN_KEY"/>
          </div>
          <div style="min-width:320px">
            <label>Serveur</label>
            <select id="guild">{GUILD_OPTS}</select>
          </div>
          <div style="min-width:160px; margin-top:22px">
            <button class="btn primary" onclick="loadAll()">Charger</button>
            <a href="https://discord.com/oauth2/authorize?client_id=1473406484393103420&permissions=8&integration_type=0&scope=bot+applications.commands" target="_blank" style="margin-left:12px;">
  <button class="btn">
    ➕ Inviter Safe
  </button>
</a>
          </div>
        </div>
      </div>

      <div class="card" style="min-width:260px">
        <div class="title">Infos</div>
        <div class="hint">
          <div>Guilds visibles: <b id="guildCount">0</b></div>
          <div>Bot: <b id="botState">unknown</b></div>
          <div>Uptime: <b id="uptime">-</b></div>
        </div>
      </div>
    </div>

    <!-- DASHBOARD -->
    <section id="tab-dashboard" class="tab active">
      <div class="grid3">
        <div class="card">
          <div class="title">Quick actions</div>
          <div class="row">
            <button class="btn danger" onclick="panelAction('lockdown', true)">Lockdown</button>
            <button class="btn" onclick="panelAction('lockdown', false)">Unlock</button>
            <button class="btn danger" onclick="panelAction('purge_global', 20)">Purge global (20)</button>
          </div>
          <div class="hint">Fonctionne seulement si le bot est connecté au serveur sélectionné.</div>
        </div>

        <div class="card">
          <div class="title">Sanctions rapides</div>
          <div class="row">
            <div style="min-width:260px">
              <label>Recherche membre</label>
              <input id="member_search" placeholder="pseudo / @username / id" oninput="filterMembers()" />
              <div class="hint" id="members_status">Clique “Charger” pour charger la liste.</div>
            </div>
            <div style="min-width:360px">
              <label>Membre</label>
              <select id="target_select" onchange="onMemberSelect()">
                <option value="">— Sélectionner un membre —</option>
              </select>
            </div>
            <div style="min-width:260px">
              <label>ID (fallback)</label>
              <input id="target" placeholder="ex: 1234567890"/>
            </div>
            <div style="min-width:320px">
              <label>Raison</label>
              <input id="reason" placeholder="optionnel"/>
            </div>
          </div>

          <div class="row" style="margin-top:10px">
            <div style="min-width:220px">
              <label>Durée timeout</label>
              <select id="timeout_preset" onchange="onTimeoutPreset()">
                <option value="1m">1m</option>
                <option value="10m">10m</option>
                <option value="30m">30m</option>
                <option value="1h" selected>1h</option>
                <option value="6h">6h</option>
                <option value="12h">12h</option>
                <option value="1d">1j</option>
                <option value="3d">3j</option>
                <option value="1w">1s</option>
                <option value="2w">2s</option>
                <option value="1mo">1mo</option>
                <option value="3mo">3mo</option>
                <option value="custom">Custom…</option>
              </select>
            </div>
            <div style="min-width:220px">
              <label>Custom</label>
              <input id="timeout_custom" placeholder="ex: 1h30m" disabled />
            </div>
            <div style="flex:1"></div>
          </div>

          <div class="row" style="margin-top:10px">
            <button class="btn" onclick="panelPunishEx('timeout')">Timeout</button>
            <button class="btn" onclick="panelPunishEx('untimeout')">Un-timeout</button>
            <button class="btn" onclick="panelPunishEx('warn')">Warn</button>
            <button class="btn danger" onclick="panelPunishEx('kick')">Kick</button>
            <button class="btn danger" onclick="panelPunishEx('ban')">Ban</button>
            <button class="btn" onclick="panelPunishEx('unban')">Unban</button>
          </div>
        </div>

        <div class="card">
          <div class="title">Santé</div>
          <div class="hint" id="healthBox">Clique “Charger” pour afficher l’état.</div>
        </div>
      </div>
    </section>

    <!-- CONFIG -->
    <section id="tab-config" class="tab">
      <div class="grid">
        <div class="card">
          <div class="title">Automod</div>
          <label><input type="checkbox" id="automod_enabled"/> Activé</label>
          <label><input type="checkbox" id="anti_invite"/> Anti-invite</label>
          <label><input type="checkbox" id="anti_link"/> Anti-link</label>
          <label><input type="checkbox" id="anti_caps"/> Anti-caps</label>
          <label>Seuil caps (%)</label>
          <input id="caps_threshold" placeholder="70"/>
          <label>Spam interval (sec)</label>
          <input id="spam_interval_sec" placeholder="2.0"/>
          <label>Spam burst</label>
          <input id="spam_burst" placeholder="5"/>
          <label>Timeout spam (min)</label>
          <input id="spam_timeout_min" placeholder="10"/>
        </div>

        <div class="card">
          <div class="title">Channels & messages</div>
          <label>Modlog channel ID</label>
          <input id="modlog_channel_id" placeholder="ID salon logs"/>
          <label>Welcome channel ID</label>
          <input id="welcome_channel_id" placeholder="ID salon bienvenue"/>
          <label>Welcome message</label>
          <input id="welcome_message" placeholder="Bienvenue {user} sur {server} !"/>
          <label>Goodbye channel ID</label>
          <input id="goodbye_channel_id" placeholder="ID salon départ"/>
          <label>Goodbye message</label>
          <input id="goodbye_message" placeholder="Au revoir {user}"/>
          <label>Suggestion channel ID</label>
          <input id="suggestion_channel_id" placeholder="ID salon suggestions"/>
          <div class="hint">Placeholders: {user} et {server} (Discord remplacer automatiquement côté bot).</div>
          <div class="row" style="margin-top:12px">
            <button class="btn primary" onclick="saveCfg()">Sauvegarder</button>
          </div>
        </div>
      </div>

      <div class="grid" style="margin-top:14px">
        <div class="card">
          <div class="title">Systèmes</div>
          <label><input type="checkbox" id="leveling_enabled"/> Levels/XP activé</label>
          <label><input type="checkbox" id="economy_enabled"/> Économie activée</label>
          <div class="row" style="margin-top:12px">
            <button class="btn primary" onclick="saveSystems()">Sauvegarder</button>
          </div>
        </div>

        <div class="card">
          <div class="title">Shop</div>
          <div class="row">
            <button class="btn" onclick="loadShop()">Afficher shop</button>
            <button class="btn" onclick="seedShop()">Créer shop par défaut</button>
          </div>
          <div class="hint" id="shopBox">—</div>
        </div>
      </div>
    </section>

    <!-- MODERATION -->
    <section id="tab-moderation" class="tab">
      <div class="grid">
        <div class="card">
          <div class="title">Infractions</div>
          <label>ID utilisateur</label>
          <input id="inf_user" placeholder="123456..."/>
          <label>Limit</label>
          <input id="inf_limit" placeholder="20"/>
          <div class="row" style="margin-top:12px">
            <button class="btn" onclick="loadInfractions()">Charger</button>
          </div>
          <div class="console" id="infBox">—</div>
        </div>

        <div class="card">
          <div class="title">Actions salon</div>
          <div class="row">
            <button class="btn" onclick="panelAction('lockdown', true)">Lockdown</button>
            <button class="btn" onclick="panelAction('lockdown', false)">Unlock</button>
            <button class="btn danger" onclick="panelAction('purge_global', 50)">Purge global (50)</button>
          </div>
          <div class="hint">Certaines actions demandent permissions Discord.</div>
        </div>
      </div>
    </section>

    <!-- EMBED BUILDER -->
    <section id="tab-embed" class="tab">
      <div class="grid">
        <div class="card">
          <div class="title">Créer un embed</div>
          <label>Channel ID</label>
          <input id="embed_channel_id" placeholder="ID salon où envoyer"/>
          <label>Titre</label>
          <input id="embed_title" placeholder="Titre"/>
          <label>Description</label>
          <textarea id="embed_desc" placeholder="Texte..."></textarea>
          <div class="row">
            <div>
              <label>Couleur (hex)</label>
              <input id="embed_color" placeholder="#5AA7FF"/>
            </div>
            <div>
              <label>Thumbnail URL</label>
              <input id="embed_thumb" placeholder="https://..."/>
            </div>
          </div>
          <label>Image URL</label>
          <input id="embed_image" placeholder="https://..."/>
          <label>Footer</label>
          <input id="embed_footer" placeholder="Footer..."/>

          <div class="card" style="margin-top:14px; background: rgba(255,255,255,.03)">
            <div class="title">Fields (JSON)</div>
            <div class="hint">Format: [{"name":"Nom","value":"Valeur","inline":true}]</div>
            <textarea id="embed_fields" placeholder='[{"name":"Règle 1","value":"Respect","inline":true}]'></textarea>
          </div>

          <div class="row" style="margin-top:12px">
            <button class="btn" onclick="previewEmbed()">Preview</button>
            <button class="btn primary" onclick="sendEmbed()">Envoyer</button>
          </div>
          <div class="hint" id="embedMsg">—</div>
        </div>

        <div class="card">
          <div class="title">Preview</div>
          <div class="preview" id="embedPreview">
            <div class="p-title">Titre</div>
            <div class="p-desc">Description…</div>
            <div class="hint">Fields ici</div>
          </div>
        </div>
      </div>
    </section>

    <!-- GIVEAWAYS -->
    <section id="tab-giveaway" class="tab">
      <div class="grid">
        <div class="card">
          <div class="title">Créer un giveaway</div>
          <label>Channel ID</label>
          <input id="gw_channel_id" placeholder="ID salon"/>
          <label>Prix</label>
          <input id="gw_prize" placeholder="Nitro / rôle / etc"/>
          <div class="row">
            <div>
              <label>Durée (ex: 10m, 2h, 1d)</label>
              <input id="gw_duration" placeholder="10m"/>
            </div>
            <div>
              <label>Gagnants</label>
              <input id="gw_winners" placeholder="1"/>
            </div>
          </div>
          <label>Emoji (optionnel)</label>
          <input id="gw_emoji" placeholder="🎉"/>
          <div class="row" style="margin-top:12px">
            <button class="btn primary" onclick="createGiveaway()">Créer</button>
          </div>
          <div class="hint" id="gwMsg">—</div>
        </div>

        <div class="card">
          <div class="title">Note</div>
          <div class="hint">
            Le bot poste un message giveaway, les gens réagissent avec l’emoji.<br/>
            À la fin, le bot choisit au hasard et annonce le(s) gagnant(s).
          </div>
        </div>
      </div>
    </section>

    <!-- TOOLS -->
    <section id="tab-tools" class="tab">
      <div class="grid">
        <div class="card">
          <div class="title">Outils</div>
          <div class="row">
            <button class="btn" onclick="fetchInfo()">Rafraîchir infos</button>
            <button class="btn" onclick="loadLeaderboard()">Leaderboard XP</button>
          </div>
          <div class="console" id="toolBox">—</div>
        </div>

        <div class="card">
          <div class="title">Conseil</div>
          <div class="hint">
            Si la liste de serveurs est vide, c’est que le bot n’est pas connecté.<br/>
            Vérifie DISCORD_TOKEN sur Render.
          </div>
        </div>
      </div>
    </section>

    <!-- LOGS -->
    <section id="tab-logs" class="tab">
      <div class="card">
        <div class="title">Logs temps réel</div>
        <div class="console" id="console">En attente…</div>
      </div>
    </section>

  </main>
</div>

<script>
let ALL_MEMBERS = [];

const navButtons = document.querySelectorAll('.nav button');
navButtons.forEach(btn => {
  btn.addEventListener('click', () => {
    navButtons.forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const tabId = btn.getAttribute('data-tab');
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.getElementById(tabId).classList.add('active');
  });
});

function keyVal(){ return document.getElementById('key').value; }
function guildVal(){ return document.getElementById('guild').value; }

function logBox(id, text){
  document.getElementById(id).innerHTML = text;
}

async function api(path, payload){
  const r = await fetch(path, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  });
  return await r.json();
}

async function loadAll(){
  await fetchInfo();
  await loadCfg();
  await loadShop();
  await loadLogsOnce();
  await loadMembers();
}

async function fetchInfo(){
  const d = await api('/api/info', {k:keyVal()});
  if(d.error) return;
  document.getElementById('guildCount').innerText = d.guilds || 0;
  document.getElementById('botState').innerText = d.bot_connected ? 'connected' : 'offline';
  document.getElementById('uptime').innerText = d.uptime || '-';
  document.getElementById('healthBox').innerHTML =
    (d.bot_connected ? '<span class="ok">● Bot connecté</span>' : '<span class="bad">● Bot offline</span>') +
    '<br/><span class="hint">Panel: OK</span>';
}

async function panelAction(action, val){
  const d = await api('/api/run', {k:keyVal(), g:guildVal(), action:action, val:val});
  if(d.error) alert(d.error);
}

async function panelPunish(action){
  const target = document.getElementById('target').value.trim();
  const reason = document.getElementById('reason').value.trim() || 'Via Panel';
  const d = await api('/api/run', {k:keyVal(), g:guildVal(), action:action, target:target, reason:reason});
  if(d.error) alert(d.error);
}


async function loadMembers(){
  const box = document.getElementById('members_status');
  const sel = document.getElementById('target_select');
  if(!sel) return;
  sel.innerHTML = '<option value="">— Chargement… —</option>';
  ALL_MEMBERS = [];
  const d = await api('/api/members/list', {k:keyVal(), g:guildVal()});
  if(d.error){
    sel.innerHTML = '<option value="">— Aucun membre —</option>';
    if(box) box.innerText = 'Erreur: ' + d.error;
    console.warn('members/list error', d);
    return;
  }
  if(!d.ok || !Array.isArray(d.members)){
    sel.innerHTML = '<option value="">— Aucun membre —</option>';
    if(box) box.innerText = 'Erreur: réponse invalide.';
    console.warn('members/list invalid', d);
    return;
  }
  ALL_MEMBERS = d.members;
  renderMembers(ALL_MEMBERS);
  if(box) box.innerText = `✅ ${d.count || ALL_MEMBERS.length} membre(s) chargé(s) (${d.used||'cache'})`;
}

function renderMembers(list){
  const sel = document.getElementById('target_select');
  if(!sel) return;
  const maxShow = 200;
  const slice = list.slice(0, maxShow);
  sel.innerHTML = '<option value="">— Sélectionner un membre —</option>' +
    slice.map(m => `<option value="${m.id}">${escapeHtml(m.display)}</option>`).join('');
  if(list.length > maxShow){
    sel.innerHTML += `<option value="">— (+${list.length-maxShow} autres, utilise la recherche) —</option>`;
  }
}

function filterMembers(){
  const q = (document.getElementById('member_search')?.value || '').trim().toLowerCase();
  if(!q){
    renderMembers(ALL_MEMBERS);
    return;
  }
  const filtered = ALL_MEMBERS.filter(m => (m.display||'').toLowerCase().includes(q) || (m.id||'').includes(q));
  renderMembers(filtered);
}

function onMemberSelect(){
  const sel = document.getElementById('target_select');
  const inp = document.getElementById('target');
  if(sel && inp && sel.value){
    inp.value = sel.value;
  }
}

function onTimeoutPreset(){
  const preset = document.getElementById('timeout_preset')?.value;
  const custom = document.getElementById('timeout_custom');
  if(!custom) return;
  if(preset === 'custom'){
    custom.disabled = false;
    custom.focus();
  } else {
    custom.disabled = true;
    custom.value = '';
  }
}

async function panelPunishEx(action){
  const target = document.getElementById('target')?.value.trim();
  const reason = document.getElementById('reason')?.value.trim() || 'Via Panel';
  let payload = {k:keyVal(), g:guildVal(), action, target, reason};
  if(action === 'timeout'){
    const preset = document.getElementById('timeout_preset')?.value || '1h';
    const custom = document.getElementById('timeout_custom')?.value.trim();
    payload.duration = (preset === 'custom' ? custom : preset);
  }
  const d = await api('/api/run', payload);
  if(d.error) alert(d.error);
  if(d.details) alert(d.details);
}

async function loadCfg(){
  const d = await api('/api/config/get', {k:keyVal(), g:guildVal()});
  if(d.error) return alert(d.error);

  document.getElementById('automod_enabled').checked = !!d.automod_enabled;
  document.getElementById('anti_invite').checked = !!d.anti_invite;
  document.getElementById('anti_link').checked = !!d.anti_link;
  document.getElementById('anti_caps').checked = !!d.anti_caps;
  document.getElementById('caps_threshold').value = d.caps_threshold ?? '';
  document.getElementById('spam_interval_sec').value = d.spam_interval_sec ?? '';
  document.getElementById('spam_burst').value = d.spam_burst ?? '';
  document.getElementById('spam_timeout_min').value = d.spam_timeout_min ?? '';

  document.getElementById('modlog_channel_id').value = d.modlog_channel_id ?? '';
  document.getElementById('welcome_channel_id').value = d.welcome_channel_id ?? '';
  document.getElementById('welcome_message').value = d.welcome_message ?? '';
  document.getElementById('goodbye_channel_id').value = d.goodbye_channel_id ?? '';
  document.getElementById('goodbye_message').value = d.goodbye_message ?? '';
  document.getElementById('suggestion_channel_id').value = d.suggestion_channel_id ?? '';

  document.getElementById('leveling_enabled').checked = !!d.leveling_enabled;
  document.getElementById('economy_enabled').checked = !!d.economy_enabled;
}

async function saveCfg(){
  const payload = {
    k:keyVal(), g:guildVal(),
    automod_enabled: document.getElementById('automod_enabled').checked,
    anti_invite: document.getElementById('anti_invite').checked,
    anti_link: document.getElementById('anti_link').checked,
    anti_caps: document.getElementById('anti_caps').checked,
    caps_threshold: document.getElementById('caps_threshold').value,
    spam_interval_sec: document.getElementById('spam_interval_sec').value,
    spam_burst: document.getElementById('spam_burst').value,
    spam_timeout_min: document.getElementById('spam_timeout_min').value,

    modlog_channel_id: document.getElementById('modlog_channel_id').value,
    welcome_channel_id: document.getElementById('welcome_channel_id').value,
    welcome_message: document.getElementById('welcome_message').value,
    goodbye_channel_id: document.getElementById('goodbye_channel_id').value,
    goodbye_message: document.getElementById('goodbye_message').value,
    suggestion_channel_id: document.getElementById('suggestion_channel_id').value
  };
  const d = await api('/api/config/set', payload);
  if(d.error) return alert(d.error);
  alert('Config sauvegardée.');
}

async function saveSystems(){
  const payload = {
    k:keyVal(), g:guildVal(),
    leveling_enabled: document.getElementById('leveling_enabled').checked,
    economy_enabled: document.getElementById('economy_enabled').checked
  };
  const d = await api('/api/config/systems', payload);
  if(d.error) return alert(d.error);
  alert('Systèmes sauvegardés.');
}

async function seedShop(){
  const d = await api('/api/shop/seed', {k:keyVal(), g:guildVal()});
  if(d.error) return alert(d.error);
  await loadShop();
}

async function loadShop(){
  const d = await api('/api/shop/list', {k:keyVal(), g:guildVal()});
  if(d.error) return;
  if(!Array.isArray(d.items)) return;
  const lines = d.items.map(i => `• ${i.name} — ${i.price} coins (key: ${i.item_key})`).join('<br/>');
  document.getElementById('shopBox').innerHTML = lines || 'Aucun item.';
}

async function loadInfractions(){
  const uid = document.getElementById('inf_user').value.trim();
  const limit = document.getElementById('inf_limit').value.trim() || '20';
  const d = await api('/api/infractions', {k:keyVal(), g:guildVal(), u:uid, limit:limit});
  if(d.error) return alert(d.error);
  if(!Array.isArray(d)) return;
  logBox('infBox', d.map(x => `#${x.id} ${x.type} ${x.created_at} — ${x.reason||''}`).join('<br/>') || 'Aucune.');
}

function safeJsonParse(s){
  try{ return JSON.parse(s); } catch(e){ return null; }
}

function previewEmbed(){
  const title = document.getElementById('embed_title').value || 'Titre';
  const desc = document.getElementById('embed_desc').value || 'Description…';
  const fields = safeJsonParse(document.getElementById('embed_fields').value || '[]') || [];
  let html = `<div class="p-title">${escapeHtml(title)}</div><div class="p-desc">${escapeHtml(desc)}</div>`;
  if(Array.isArray(fields) && fields.length){
    fields.forEach(f => {
      html += `<div class="field"><div><div class="k">${escapeHtml(f.name||'')}</div><div>${escapeHtml(f.value||'')}</div></div><div class="hint">${f.inline ? 'inline' : ''}</div></div>`;
    });
  } else {
    html += `<div class="hint">Aucun field.</div>`;
  }
  document.getElementById('embedPreview').innerHTML = html;
}

async function sendEmbed(){
  const payload = {
    k:keyVal(), g:guildVal(),
    channel_id: document.getElementById('embed_channel_id').value,
    title: document.getElementById('embed_title').value,
    description: document.getElementById('embed_desc').value,
    color: document.getElementById('embed_color').value,
    thumbnail: document.getElementById('embed_thumb').value,
    image: document.getElementById('embed_image').value,
    footer: document.getElementById('embed_footer').value,
    fields_json: document.getElementById('embed_fields').value
  };
  const d = await api('/api/embed/send', payload);
  document.getElementById('embedMsg').innerText = d.error ? ('Erreur: ' + d.error) : ('OK: ' + d.details);
}

async function createGiveaway(){
  const payload = {
    k:keyVal(), g:guildVal(),
    channel_id: document.getElementById('gw_channel_id').value,
    prize: document.getElementById('gw_prize').value,
    duration: document.getElementById('gw_duration').value,
    winners: document.getElementById('gw_winners').value,
    emoji: document.getElementById('gw_emoji').value
  };
  const d = await api('/api/giveaway/create', payload);
  document.getElementById('gwMsg').innerText = d.error ? ('Erreur: ' + d.error) : ('OK: ' + d.details);
}

async function loadLeaderboard(){
  const d = await api('/api/xp/leaderboard', {k:keyVal(), g:guildVal()});
  if(d.error) return alert(d.error);
  const lines = (d.items||[]).map((x,i)=> `${i+1}. ${x.user_id} — lvl ${x.level} (${x.xp} xp)`).join('<br/>');
  logBox('toolBox', lines || 'Aucun.');
}

async function loadLogsOnce(){
  const d = await fetch('/api/logs');
  const arr = await d.json();
  if(Array.isArray(arr)) {
    document.getElementById('console').innerHTML = arr.slice().reverse().map(x=>`<div>${escapeHtml(x)}</div>`).join('');
  }
}

setInterval(async ()=>{
  try{
    const r = await fetch('/api/logs');
    const arr = await r.json();
    if(Array.isArray(arr)) {
      document.getElementById('console').innerHTML = arr.slice().reverse().map(x=>`<div>${escapeHtml(x)}</div>`).join('');
    }
  }catch(e){}
}, 2500);

function escapeHtml(s){
  return (s||'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
}
</script>
</body>
</html>
"""

@app.get("/leviathan", response_class=HTMLResponse)
async def panel():
    guilds = list(getattr(bot, "guilds", []) or [])
    if not guilds:
        guild_opts = "<option value='0'>Aucun serveur (bot offline / pas prêt)</option>"
    else:
        guild_opts = "".join([f"<option value='{g.id}'>{g.name}</option>" for g in guilds])
    html = PANEL_HTML.replace("{GUILD_OPTS}", guild_opts)
    return HTMLResponse(html)

# =========================================================
# PANEL API
# =========================================================
def auth(data: Dict[str, Any]) -> Optional[str]:
    if data.get("k") != ADMIN_KEY:
        return "MOT DE PASSE INCORRECT"
    return None

@app.post("/api/info")
async def api_info(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({"error": auth(data)}, status_code=403)
    uptime = int(time.time() - START_TIME)
    return {
        "bot_connected": bool(bot.user),
        "guilds": len(getattr(bot, "guilds", []) or []),
        "uptime": f"{uptime}s"
    }

@app.get("/api/logs")
async def api_logs():
    return action_logs

@app.post("/api/config/get")
async def api_cfg_get(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({"error": auth(data)}, status_code=403)
    gid = int(data.get("g") or 0)
    if gid <= 0:
        return JSONResponse({"error": "Guild invalide (bot offline ?)"} , status_code=400)
    return get_guild_config(gid)

def as_int_or_none(v):
    v = (v or "").strip()
    return int(v) if v.isdigit() else None

def as_float_or(v, default: float):
    try:
        return float(str(v).strip())
    except:
        return default

@app.post("/api/config/set")
async def api_cfg_set(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({"error": auth(data)}, status_code=403)
    gid = int(data.get("g") or 0)
    if gid <= 0:
        return JSONResponse({"error": "Guild invalide"} , status_code=400)

    set_guild_config(
        gid,
        automod_enabled=1 if data.get("automod_enabled") else 0,
        anti_invite=1 if data.get("anti_invite") else 0,
        anti_link=1 if data.get("anti_link") else 0,
        anti_caps=1 if data.get("anti_caps") else 0,
        caps_threshold=int(as_int_or_none(data.get("caps_threshold")) or 70),
        spam_interval_sec=as_float_or(data.get("spam_interval_sec"), 2.0),
        spam_burst=int(as_int_or_none(data.get("spam_burst")) or 5),
        spam_timeout_min=int(as_int_or_none(data.get("spam_timeout_min")) or 10),

        modlog_channel_id=as_int_or_none(data.get("modlog_channel_id")),
        welcome_channel_id=as_int_or_none(data.get("welcome_channel_id")),
        welcome_message=(data.get("welcome_message") or "").strip() or None,
        goodbye_channel_id=as_int_or_none(data.get("goodbye_channel_id")),
        goodbye_message=(data.get("goodbye_message") or "").strip() or None,
        suggestion_channel_id=as_int_or_none(data.get("suggestion_channel_id")),
    )
    add_log(f"Panel: config saved guild={gid}")
    return {"status": "ok"}

@app.post("/api/config/systems")
async def api_cfg_systems(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({"error": auth(data)}, status_code=403)
    gid = int(data.get("g") or 0)
    if gid <= 0:
        return JSONResponse({"error": "Guild invalide"} , status_code=400)
    set_guild_config(
        gid,
        leveling_enabled=1 if data.get("leveling_enabled") else 0,
        economy_enabled=1 if data.get("economy_enabled") else 0,
    )
    add_log(f"Panel: systems saved guild={gid}")
    return {"status": "ok"}

@app.post("/api/run")
async def api_run(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({"error": auth(data)}, status_code=403)

    gid = int(data.get("g") or 0)
    action = (data.get("action") or "").strip()
    guild = bot.get_guild(gid) if gid else None
    if not guild:
        return {"error": "Serveur introuvable (bot offline ou pas dans ce serveur)."}
    try:
        if action == "lockdown":
            status = bool(data.get("val"))
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
            await send_modlog(guild, f"🔒 LOCKDOWN={status} via panel ({count} salons)")
            add_log(f"Panel: lockdown={status} guild={gid} count={count}")
            return {"details": f"OK: {count} salons modifiés."}

        if action == "purge_global":
            amount = int(data.get("val") or 20)
            total = 0
            for channel in guild.text_channels:
                try:
                    deleted = await channel.purge(limit=amount)
                    total += len(deleted)
                except:
                    continue
            await send_modlog(guild, f"🧹 Purge global via panel: {total} messages.")
            add_log(f"Panel: purge_global guild={gid} total={total}")
            return {"details": f"{total} messages supprimés."}

        if action in ("kick", "ban", "unban", "warn", "timeout", "untimeout"):
            target = str(data.get("target") or "").strip()
            reason = str(data.get("reason") or "Via Panel")

            # target can be an ID (recommended by dropdown), but keep strict here for stability
            if not target.isdigit():
                return {"error": "ID utilisateur invalide (utilise le menu déroulant ou colle un ID)."}
            uid = int(target)

            # unban uses user object
            if action == "unban":
                try:
                    user = await bot.fetch_user(uid)
                    await guild.unban(user, reason=reason)
                    add_infraction(gid, uid, None, "unban", reason)
                    await send_modlog(guild, f"✅ Unban: <@{uid}> | {reason}")
                    return {"details": "Unban OK."}
                except Exception as e:
                    return {"error": f"Unban impossible: {e}"}

            # fetch member for other actions
            try:
                member = await guild.fetch_member(uid)
            except Exception:
                return {"error": "Membre introuvable."}

            if action == "kick":
                await member.kick(reason=reason)
                add_infraction(gid, uid, None, "kick", reason)
                await send_modlog(guild, f"👢 Kick: {member.mention} | {reason}")
                return {"details": "Kick OK."}

            if action == "ban":
                await member.ban(reason=reason)
                add_infraction(gid, uid, None, "ban", reason)
                await send_modlog(guild, f"🔨 Ban: {member.mention} | {reason}")
                return {"details": "Ban OK."}

            if action == "warn":
                add_infraction(gid, uid, None, "warn", reason)
                try:
                    await member.send(f"⚠️ Avertissement sur **{guild.name}**\nRaison: {reason}")
                except Exception:
                    pass
                await send_modlog(guild, f"⚠️ Warn: {member.mention} | {reason}")
                return {"details": "Warn OK."}

            if action == "timeout":
                duration = str(data.get("duration") or "1h").strip()
                secs = parse_duration_to_seconds(duration)
                if not secs:
                    return {"error": "Durée invalide. Ex: 10m, 1h30m, 2d, 1w, 1mo, 1y"}
                # Discord hard limit ~ 28 days
                max_secs = 28 * 24 * 3600
                if secs > max_secs:
                    return {"error": "Durée trop grande (max ~28j sur Discord)."}
                await member.timeout(datetime.timedelta(seconds=secs), reason=reason)
                add_infraction(gid, uid, None, "timeout", f"{duration} | {reason}")
                await send_modlog(guild, f"🤐 Timeout {duration}: {member.mention} | {reason}")
                return {"details": f"Timeout OK ({duration})."}

            if action == "untimeout":
                await member.timeout(None, reason=reason)
                add_infraction(gid, uid, None, "untimeout", reason)
                await send_modlog(guild, f"🔈 Un-timeout: {member.mention} | {reason}")
                return {"details": "Un-timeout OK."}

        return {"error": "Action inconnue."}
    except Exception as e:
        add_log(f"Panel run error: {e}")
        return {"error": f"Erreur: {e}"}

@app.post("/api/members/list")
async def api_members_list(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({"error": auth(data)}, status_code=403)

    gid = int(data.get("g") or 0)
    guild = bot.get_guild(gid) if gid else None
    if not guild:
        return {"error": "Serveur introuvable (bot offline ou pas dans ce serveur)."}

    # Fill cache if possible
    try:
        await guild.chunk(cache=True)
    except Exception:
        pass

    members = []
    used = "cache"
    try:
        for mbr in guild.members:
            if getattr(mbr, "bot", False):
                continue
            members.append({"id": str(mbr.id), "display": f"{mbr.display_name} (@{mbr.name})"})
    except Exception:
        members = []

    if len(members) == 0:
        used = "fetch"
        try:
            async for mbr in guild.fetch_members(limit=2000):
                if getattr(mbr, "bot", False):
                    continue
                members.append({"id": str(mbr.id), "display": f"{mbr.display_name} (@{mbr.name})"})
        except Exception as e:
            return {"error": "Impossible de récupérer les membres (Members Intent + redeploy).", "detail": str(e)}

    members.sort(key=lambda x: x["display"].lower())
    return {"ok": True, "used": used, "count": len(members), "members": members[:2000]}

@app.post("/api/infractions")
async def api_infractions(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({"error": auth(data)}, status_code=403)
    gid = int(data.get("g") or 0)
    uid = int(data.get("u") or 0)
    if gid <= 0 or uid <= 0:
        return JSONResponse({"error": "guild/user invalide"}, status_code=400)
    limit = int(data.get("limit") or 20)
    return list_infractions(gid, uid, limit=limit)

@app.post("/api/shop/seed")
async def api_shop_seed(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({"error": auth(data)}, status_code=403)
    gid = int(data.get("g") or 0)
    if gid <= 0:
        return JSONResponse({"error": "Guild invalide"}, status_code=400)
    shop_seed_if_empty(gid)
    return {"status": "ok"}

@app.post("/api/shop/list")
async def api_shop_list(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({"error": auth(data)}, status_code=403)
    gid = int(data.get("g") or 0)
    if gid <= 0:
        return JSONResponse({"error": "Guild invalide"}, status_code=400)
    return {"items": shop_list(gid)}

@app.post("/api/xp/leaderboard")
async def api_xp_lb(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({"error": auth(data)}, status_code=403)
    gid = int(data.get("g") or 0)
    if gid <= 0:
        return JSONResponse({"error": "Guild invalide"}, status_code=400)
    items = xp_leaderboard(gid, limit=10)
    return {"items": items}

@app.post("/api/embed/send")
async def api_embed_send(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({"error": auth(data)}, status_code=403)

    gid = int(data.get("g") or 0)
    guild = bot.get_guild(gid) if gid else None
    if not guild:
        return {"error": "Serveur introuvable (bot offline ?)"}    

    channel_id = str(data.get("channel_id") or "").strip()
    if not channel_id.isdigit():
        return {"error": "Channel ID invalide."}
    ch = guild.get_channel(int(channel_id))
    if not ch:
        return {"error": "Channel introuvable sur ce serveur."}

    title = (data.get("title") or "").strip()
    desc = (data.get("description") or "").strip()
    color_raw = (data.get("color") or "").strip() or "#5AA7FF"
    thumb = (data.get("thumbnail") or "").strip() or None
    image = (data.get("image") or "").strip() or None
    footer = (data.get("footer") or "").strip() or None
    fields_json = (data.get("fields_json") or "").strip() or "[]"

    # parse color
    try:
        if color_raw.startswith("#"):
            color = int(color_raw[1:], 16)
        else:
            color = int(color_raw, 16)
    except:
        color = 0x5AA7FF

    embed = discord.Embed(title=title if title else None, description=desc if desc else None, color=color)
    if thumb:
        embed.set_thumbnail(url=thumb)
    if image:
        embed.set_image(url=image)
    if footer:
        embed.set_footer(text=footer)

    try:
        fields = json.loads(fields_json)
        if isinstance(fields, list):
            for f in fields[:25]:
                if not isinstance(f, dict):
                    continue
                n = str(f.get("name") or "")[:256]
                v = str(f.get("value") or "")[:1024]
                inline = bool(f.get("inline", False))
                if n and v:
                    embed.add_field(name=n, value=v, inline=inline)
    except:
        pass

    try:
        await ch.send(embed=embed)
        add_log(f"Panel: embed sent guild={gid} channel={channel_id}")
        return {"details": "Embed envoyé."}
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/giveaway/create")
async def api_giveaway_create(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({"error": auth(data)}, status_code=403)

    gid = int(data.get("g") or 0)
    guild = bot.get_guild(gid) if gid else None
    if not guild:
        return {"error": "Serveur introuvable (bot offline ?)"}    

    channel_id = str(data.get("channel_id") or "").strip()
    if not channel_id.isdigit():
        return {"error": "Channel ID invalide."}
    ch = guild.get_channel(int(channel_id))
    if not ch:
        return {"error": "Channel introuvable sur ce serveur."}

    prize = (data.get("prize") or "").strip()
    if not prize:
        return {"error": "Prize obligatoire."}

    duration = (data.get("duration") or "").strip()
    sec = parse_duration_to_seconds(duration)
    if not sec:
        return {"error": "Durée invalide (ex: 10m, 2h, 1d)."}

    winners_raw = str(data.get("winners") or "1").strip()
    winners = int(winners_raw) if winners_raw.isdigit() else 1
    winners = max(1, min(winners, 20))

    emoji = (data.get("emoji") or "🎉").strip() or "🎉"
    end_ts = int(time.time()) + sec
    end_dt = datetime.datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d %H:%M:%S")

    embed = discord.Embed(
        title="🎁 GIVEAWAY",
        description=f"**Prix:** {prize}\n**Gagnants:** {winners}\n**Fin:** {end_dt}\n\nRéagis avec {emoji} pour participer !",
        color=0x35FF9B
    )
    msg = await ch.send(embed=embed)
    try:
        await msg.add_reaction(emoji)
    except:
        await msg.add_reaction("🎉")
        emoji = "🎉"

    giveaway_create(gid, ch.id, msg.id, end_ts, winners, prize, emoji)
    add_log(f"Giveaway created guild={gid} channel={ch.id} msg={msg.id} end={end_ts}")
    return {"details": f"Giveaway créé (message {msg.id})."}

# =========================================================
# EVENTS
# =========================================================
@bot.event
async def on_ready():
    add_log(f"Bot connecté: {bot.user} | guilds={len(bot.guilds)}")
    try:
        await bot.tree.sync()
        add_log("Slash sync ✅")
    except Exception as e:
        add_log(f"Slash sync error: {e}")

    if not reminder_loop.is_running():
        reminder_loop.start()
    if not giveaway_loop.is_running():
        giveaway_loop.start()

@bot.event
async def on_message(message: discord.Message):
    await automod_check(message)
    await leveling_on_message(message)
    await bot.process_commands(message)

@bot.event
async def on_member_join(member: discord.Member):
    cfg = get_guild_config(member.guild.id)
    ch_id = cfg.get("welcome_channel_id")
    if ch_id:
        ch = member.guild.get_channel(int(ch_id))
        if ch:
            msg = cfg.get("welcome_message") or "Bienvenue {user} sur {server} !"
            msg = msg.replace("{user}", member.mention).replace("{server}", member.guild.name)
            try:
                await ch.send(msg)
            except:
                pass

@bot.event
async def on_member_remove(member: discord.Member):
    cfg = get_guild_config(member.guild.id)
    ch_id = cfg.get("goodbye_channel_id")
    if ch_id:
        ch = member.guild.get_channel(int(ch_id))
        if ch:
            msg = cfg.get("goodbye_message") or "Au revoir {user}"
            msg = msg.replace("{user}", str(member)).replace("{server}", member.guild.name)
            try:
                await ch.send(msg)
            except:
                pass
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

# =========================================================
# LOOPS
# =========================================================
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

@tasks.loop(seconds=15)
async def giveaway_loop():
    now_ts = int(time.time())
    due = giveaway_due(now_ts, limit=10)
    for gw in due:
        try:
            guild = bot.get_guild(int(gw["guild_id"]))
            if not guild:
                giveaway_mark_ended(int(gw["id"]))
                continue
            channel = guild.get_channel(int(gw["channel_id"]))
            if not channel:
                giveaway_mark_ended(int(gw["id"]))
                continue
            msg = await channel.fetch_message(int(gw["message_id"]))
            emoji = str(gw.get("emoji") or "🎉")
            winners = int(gw.get("winners") or 1)
            prize = str(gw.get("prize") or "Prize")

            # find reaction
            target_reaction = None
            for r in msg.reactions:
                if str(r.emoji) == emoji:
                    target_reaction = r
                    break
            if not target_reaction:
                await channel.send(f"🎁 Giveaway terminé: aucun participant. (Prix: {prize})")
                giveaway_mark_ended(int(gw["id"]))
                continue

            users = []
            async for u in target_reaction.users():
                if not u.bot:
                    users.append(u)

            if not users:
                await channel.send(f"🎁 Giveaway terminé: aucun participant. (Prix: {prize})")
                giveaway_mark_ended(int(gw["id"]))
                continue

            winners = min(winners, len(users))
            chosen = random.sample(users, winners)
            mentions = ", ".join([u.mention for u in chosen])

            await channel.send(f"🎉 **Giveaway terminé !** Prix: **{prize}**\nGagnant(s): {mentions}")
            giveaway_mark_ended(int(gw["id"]))
            add_log(f"Giveaway ended id={gw['id']} winners={winners}")
        except Exception as e:
            add_log(f"Giveaway loop error: {e}")
            giveaway_mark_ended(int(gw["id"]))

# =========================================================
# COMMANDS (PREFIX !)
# =========================================================
def is_admin():
    async def predicate(ctx: commands.Context):
        return ctx.author.guild_permissions.administrator
    return commands.check(predicate)

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
    try:
        await ctx.message.delete()
    except:
        pass
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
    await member.timeout(datetime.timedelta(seconds=sec), reason=reason)
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
    lines = [f"#{r['id']} • {r['type']} • {r['created_at'][:19].replace('T',' ')} • {r.get('reason') or ''}" for r in rows]
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
        return await ctx.send('Utilise: `!poll "Question" option1 | option2 | option3` (2 à 10 options)')
    emojis = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    desc = "\n".join([f"{emojis[i]} {opts[i]}" for i in range(len(opts))])
    embed = discord.Embed(title=question, description=desc, color=0x5AA7FF)
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

# Suggestions
@bot.command()
async def suggest(ctx, *, text: str):
    cfg = get_guild_config(ctx.guild.id)
    ch_id = cfg.get("suggestion_channel_id")
    if not ch_id:
        return await ctx.send("❌ suggestion_channel_id n’est pas configuré dans le panel.")
    ch = ctx.guild.get_channel(int(ch_id))
    if not ch:
        return await ctx.send("❌ Salon suggestions introuvable.")
    embed = discord.Embed(title="💡 Suggestion", description=text, color=0xFFD166)
    embed.set_footer(text=f"Par {ctx.author} • ID {ctx.author.id}")
    msg = await ch.send(embed=embed)
    try:
        await msg.add_reaction("👍")
        await msg.add_reaction("👎")
    except:
        pass
    await ctx.send("✅ Suggestion envoyée.")

# Leveling commands
@bot.command()
async def rank(ctx, member: Optional[discord.Member] = None):
    m = member or ctx.author
    row = xp_get(ctx.guild.id, m.id)
    xp = int(row["xp"])
    lvl = int(row["level"])
    next_need = xp_needed_for_level(lvl + 1)
    await ctx.send(f"📈 {m.mention} — niveau **{lvl}** | XP **{xp}** | prochain niveau à **{next_need}** XP")

@bot.command()
async def leaderboard(ctx):
    items = xp_leaderboard(ctx.guild.id, limit=10)
    if not items:
        return await ctx.send("Aucun XP.")
    lines = []
    for i, it in enumerate(items, start=1):
        lines.append(f"{i}. <@{it['user_id']}> — lvl {it['level']} ({it['xp']} xp)")
    await ctx.send("🏆 **Leaderboard XP**\n" + "\n".join(lines))

# Economy commands
@bot.command()
async def balance(ctx, member: Optional[discord.Member] = None):
    cfg = get_guild_config(ctx.guild.id)
    if not cfg.get("economy_enabled", 1):
        return await ctx.send("Économie désactivée.")
    m = member or ctx.author
    row = econ_get(ctx.guild.id, m.id)
    await ctx.send(f"💰 {m.mention} — **{row['balance']}** coins")

@bot.command()
async def daily(ctx):
    cfg = get_guild_config(ctx.guild.id)
    if not cfg.get("economy_enabled", 1):
        return await ctx.send("Économie désactivée.")
    row = econ_get(ctx.guild.id, ctx.author.id)
    now = int(time.time())
    cooldown = 24 * 3600
    if now - int(row["last_daily_ts"]) < cooldown:
        remain = cooldown - (now - int(row["last_daily_ts"]))
        hrs = int(remain // 3600)
        mins = int((remain % 3600) // 60)
        return await ctx.send(f"⏳ Reviens dans {hrs}h{mins}m pour ton daily.")
    gain = random.randint(100, 200)
    econ_set(ctx.guild.id, ctx.author.id, int(row["balance"]) + gain, now)
    await ctx.send(f"🎁 Daily: +{gain} coins !")

@bot.command()
async def pay(ctx, member: discord.Member, amount: int):
    cfg = get_guild_config(ctx.guild.id)
    if not cfg.get("economy_enabled", 1):
        return await ctx.send("Économie désactivée.")
    if amount <= 0:
        return await ctx.send("Montant invalide.")
    if member.bot:
        return await ctx.send("Impossible.")
    me = econ_get(ctx.guild.id, ctx.author.id)
    if int(me["balance"]) < amount:
        return await ctx.send("Solde insuffisant.")
    you = econ_get(ctx.guild.id, member.id)
    econ_set(ctx.guild.id, ctx.author.id, int(me["balance"]) - amount, int(me["last_daily_ts"]))
    econ_set(ctx.guild.id, member.id, int(you["balance"]) + amount, int(you["last_daily_ts"]))
    await ctx.send(f"✅ {ctx.author.mention} a payé {member.mention} **{amount}** coins.")

@bot.command()
async def shop(ctx):
    cfg = get_guild_config(ctx.guild.id)
    if not cfg.get("economy_enabled", 1):
        return await ctx.send("Économie désactivée.")
    items = shop_list(ctx.guild.id)
    if not items:
        return await ctx.send("Shop vide (utilise le panel > Shop > créer par défaut).")
    lines = [f"• `{i['item_key']}` — **{i['price']}** coins — {i['name']}" for i in items]
    await ctx.send("🛒 **Shop**\n" + "\n".join(lines))

@bot.command()
async def buy(ctx, item_key: str):
    cfg = get_guild_config(ctx.guild.id)
    if not cfg.get("economy_enabled", 1):
        return await ctx.send("Économie désactivée.")
    item_key = (item_key or "").strip()
    items = {i["item_key"]: i for i in shop_list(ctx.guild.id)}
    if item_key not in items:
        return await ctx.send("Item introuvable.")
    item = items[item_key]
    row = econ_get(ctx.guild.id, ctx.author.id)
    bal = int(row["balance"])
    price = int(item["price"])
    if bal < price:
        return await ctx.send("Solde insuffisant.")
    econ_set(ctx.guild.id, ctx.author.id, bal - price, int(row["last_daily_ts"]))
    await ctx.send(f"✅ Achat: **{item['name']}** pour {price} coins. (symbolique, à gérer côté staff)")

# Giveaway command
@bot.command()
@commands.has_permissions(manage_guild=True)
async def giveaway(ctx, duration: str, winners: int, *, prize: str):
    sec = parse_duration_to_seconds(duration)
    if not sec:
        return await ctx.send("Durée invalide (ex: 10m, 2h, 1d).")
    winners = max(1, min(int(winners), 20))
    end_ts = int(time.time()) + sec
    end_dt = datetime.datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d %H:%M:%S")
    emoji = "🎉"
    embed = discord.Embed(
        title="🎁 GIVEAWAY",
        description=f"**Prix:** {prize}\n**Gagnants:** {winners}\n**Fin:** {end_dt}\n\nRéagis avec {emoji} pour participer !",
        color=0x35FF9B
    )
    msg = await ctx.send(embed=embed)
    await msg.add_reaction(emoji)
    giveaway_create(ctx.guild.id, ctx.channel.id, msg.id, end_ts, winners, prize, emoji)
    await ctx.send("✅ Giveaway créé.")

# =========================================================
# SLASH COMMANDS (/)
# =========================================================
@bot.tree.command(name="ping", description="Voir la latence du bot")
async def slash_ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong: {round(bot.latency*1000)}ms", ephemeral=True)

@bot.tree.command(name="rank", description="Voir ton niveau/XP")
async def slash_rank(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    m = user or interaction.user
    if not interaction.guild_id:
        return await interaction.response.send_message("Pas de serveur.", ephemeral=True)
    row = xp_get(interaction.guild_id, m.id)
    xp = int(row["xp"])
    lvl = int(row["level"])
    next_need = xp_needed_for_level(lvl + 1)
    await interaction.response.send_message(f"📈 {m.mention} — niveau **{lvl}** | XP **{xp}** | prochain niveau à **{next_need}** XP", ephemeral=True)

@bot.tree.command(name="balance", description="Voir ton solde")
async def slash_balance(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    if not interaction.guild_id:
        return await interaction.response.send_message("Pas de serveur.", ephemeral=True)
    cfg = get_guild_config(interaction.guild_id)
    if not cfg.get("economy_enabled", 1):
        return await interaction.response.send_message("Économie désactivée.", ephemeral=True)
    m = user or interaction.user
    row = econ_get(interaction.guild_id, m.id)
    await interaction.response.send_message(f"💰 {m.mention} — **{row['balance']}** coins", ephemeral=True)

@bot.tree.command(name="ticket", description="Créer un ticket support")
async def slash_ticket(interaction: discord.Interaction, subject: str = "Support"):
    if not interaction.guild:
        return await interaction.response.send_message("Pas de serveur.", ephemeral=True)
    cfg = get_guild_config(interaction.guild.id)
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

# =========================================================
# MAIN (panel never dies if bot fails)
# =========================================================
async def start_bot_safely():
    if not DISCORD_TOKEN:
        add_log("❌ DISCORD_TOKEN manquant: bot offline, panel OK.")
        return
    try:
        await bot.start(DISCORD_TOKEN)
    except Exception as e:
        add_log(f"❌ Bot crash: {e} (panel reste accessible)")

async def main():
    db_init()
    asyncio.create_task(start_bot_safely())

    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()



# =========================================================
# ULTRA ADDONS (panel + automod + economy + tickets + analytics)
# =========================================================
from collections import defaultdict, deque

RECENT_USER_MESSAGES: Dict[Tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=8))
AFK_USERS: Dict[Tuple[int, int], str] = {}
LAST_DELETED: Dict[Tuple[int, int], Dict[str, Any]] = {}
JOIN_TRACKER: Dict[int, deque] = defaultdict(lambda: deque(maxlen=20))
STARBOARD_CACHE: set = set()


def db_init_plus():
    con = db_connect()
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS addon_config (
        guild_id INTEGER PRIMARY KEY,
        anti_mention_spam INTEGER DEFAULT 0,
        mention_threshold INTEGER DEFAULT 5,
        anti_bad_words INTEGER DEFAULT 0,
        anti_duplicate INTEGER DEFAULT 0,
        anti_ghost_ping INTEGER DEFAULT 0,
        starboard_enabled INTEGER DEFAULT 0,
        starboard_channel_id INTEGER,
        starboard_threshold INTEGER DEFAULT 3,
        snipe_enabled INTEGER DEFAULT 1,
        dm_welcome_enabled INTEGER DEFAULT 0,
        autorole_enabled INTEGER DEFAULT 0,
        autorole_id INTEGER,
        suggest_autoreact INTEGER DEFAULT 1,
        raid_join_enabled INTEGER DEFAULT 0,
        raid_join_threshold INTEGER DEFAULT 5,
        raid_join_window_sec INTEGER DEFAULT 15,
        econ_daily_min INTEGER DEFAULT 100,
        econ_daily_max INTEGER DEFAULT 200,
        econ_work_min INTEGER DEFAULT 50,
        econ_work_max INTEGER DEFAULT 120,
        ticket_panel_channel_id INTEGER,
        ticket_panel_message_id INTEGER,
        transcript_channel_id INTEGER
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS bad_words (
        guild_id INTEGER NOT NULL,
        word TEXT NOT NULL,
        PRIMARY KEY (guild_id, word)
    )
    """)
    con.commit()
    con.close()


def get_addon_config(guild_id: int) -> Dict[str, Any]:
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM addon_config WHERE guild_id=?", (guild_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT OR IGNORE INTO addon_config(guild_id) VALUES (?)", (guild_id,))
        con.commit()
        cur.execute("SELECT * FROM addon_config WHERE guild_id=?", (guild_id,))
        row = cur.fetchone()
    con.close()
    return dict(row)


def set_addon_config(guild_id: int, **kwargs):
    if not kwargs:
        return
    con = db_connect()
    cur = con.cursor()
    keys = []
    vals = []
    for k, v in kwargs.items():
        keys.append(f"{k}=?")
        vals.append(v)
    vals.append(guild_id)
    cur.execute(f"UPDATE addon_config SET {', '.join(keys)} WHERE guild_id=?", tuple(vals))
    con.commit()
    con.close()


def badwords_list(guild_id: int) -> List[str]:
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT word FROM bad_words WHERE guild_id=? ORDER BY word ASC", (guild_id,))
    rows = [r['word'] for r in cur.fetchall()]
    con.close()
    return rows


def badword_add(guild_id: int, word: str):
    word = (word or '').strip().lower()
    if not word:
        return
    con = db_connect()
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO bad_words(guild_id, word) VALUES (?,?)", (guild_id, word))
    con.commit()
    con.close()


def badword_remove(guild_id: int, word: str):
    word = (word or '').strip().lower()
    con = db_connect()
    cur = con.cursor()
    cur.execute("DELETE FROM bad_words WHERE guild_id=? AND word=?", (guild_id, word))
    con.commit()
    con.close()


def _bool(v):
    return 1 if v else 0


def economy_cfg(guild_id: int) -> Dict[str, int]:
    cfg = get_addon_config(guild_id)
    return {
        'econ_daily_min': int(cfg.get('econ_daily_min') or 100),
        'econ_daily_max': int(cfg.get('econ_daily_max') or 200),
        'econ_work_min': int(cfg.get('econ_work_min') or 50),
        'econ_work_max': int(cfg.get('econ_work_max') or 120),
    }


def parse_ids_from_content(text: str) -> List[int]:
    ids = re.findall(r'<@!?(\d+)>', text or '')
    return [int(x) for x in ids]


_orig_on_ready = on_ready
_orig_on_message = on_message
_orig_on_member_join = on_member_join
_orig_on_member_remove = on_member_remove
_orig_on_raw_reaction_add = on_raw_reaction_add
_orig_on_raw_reaction_remove = on_raw_reaction_remove


class TicketOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Ouvrir un ticket', style=discord.ButtonStyle.green, emoji='🎫', custom_id='leviathan_ticket_open')
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message('Pas de serveur.', ephemeral=True)
        cfg = get_guild_config(interaction.guild.id)
        category = None
        if cfg.get('ticket_category_id'):
            category = interaction.guild.get_channel(int(cfg['ticket_category_id']))
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            interaction.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
        }
        name = f"ticket-{interaction.user.name}".lower().replace(' ', '-')[:90]
        channel = await interaction.guild.create_text_channel(name=name, overwrites=overwrites, category=category)
        await channel.send(f"🎫 Ticket créé pour {interaction.user.mention}\nUtilise `!closeticket` pour fermer.")
        await interaction.response.send_message(f"✅ Ticket créé: {channel.mention}", ephemeral=True)
        await send_modlog(interaction.guild, f"🎫 Ticket via panel: {channel.mention} par {interaction.user.mention}")


@bot.event
async def on_ready():
    await _orig_on_ready()
    try:
        bot.add_view(TicketOpenView())
    except Exception:
        pass


async def extra_automod(message: discord.Message):
    if not message.guild or message.author.bot:
        return False
    addon = get_addon_config(message.guild.id)
    content = message.content or ''
    if addon.get('anti_bad_words'):
        lowered = content.lower()
        words = badwords_list(message.guild.id)
        hit = next((w for w in words if w and w in lowered), None)
        if hit and not message.author.guild_permissions.manage_messages:
            try:
                await message.delete()
            except Exception:
                pass
            add_infraction(message.guild.id, message.author.id, None, 'badword', hit)
            await send_modlog(message.guild, f"🤬 Anti bad-word: mot détecté chez {message.author.mention} dans {message.channel.mention}")
            return True
    if addon.get('anti_mention_spam'):
        threshold = int(addon.get('mention_threshold') or 5)
        mention_count = len(message.mentions) + len(message.role_mentions)
        if mention_count >= threshold and not message.author.guild_permissions.manage_messages:
            try:
                await message.delete()
            except Exception:
                pass
            await send_modlog(message.guild, f"📣 Mention spam: {message.author.mention} ({mention_count} mentions) dans {message.channel.mention}")
            return True
    if addon.get('anti_duplicate'):
        key = (message.guild.id, message.author.id)
        dq = RECENT_USER_MESSAGES[key]
        normalized = re.sub(r'\s+', ' ', content.strip().lower())
        dq.append((time.time(), normalized))
        recent_same = [x for t, x in dq if time.time() - t <= 30 and x and x == normalized]
        if len(recent_same) >= 3 and not message.author.guild_permissions.manage_messages:
            try:
                await message.delete()
            except Exception:
                pass
            await send_modlog(message.guild, f"♻️ Duplicate spam: {message.author.mention} dans {message.channel.mention}")
            return True
    return False


@bot.event
async def on_message(message: discord.Message):
    if message.guild and not message.author.bot:
        if (message.guild.id, message.author.id) in AFK_USERS:
            AFK_USERS.pop((message.guild.id, message.author.id), None)
            try:
                await message.channel.send(f"👋 {message.author.mention}, tu n'es plus AFK.", delete_after=8)
            except Exception:
                pass
        for u in message.mentions:
            reason = AFK_USERS.get((message.guild.id, u.id))
            if reason:
                try:
                    await message.channel.send(f"💤 {u.mention} est AFK: {reason}", delete_after=10)
                except Exception:
                    pass
    blocked = await extra_automod(message)
    if blocked:
        return
    await _orig_on_message(message)


@bot.event
async def on_message_delete(message: discord.Message):
    if not message.guild or message.author.bot:
        return
    addon = get_addon_config(message.guild.id)
    if addon.get('snipe_enabled', 1):
        LAST_DELETED[(message.guild.id, message.channel.id)] = {
            'author': str(message.author),
            'author_id': message.author.id,
            'content': message.content or '',
            'attachments': [a.url for a in message.attachments[:3]],
        }


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not before.guild or before.author.bot or before.content == after.content:
        return
    await send_modlog(before.guild, f"✏️ Edit: {before.author.mention} dans {before.channel.mention}\nAvant: {before.content[:300]}\nAprès: {after.content[:300]}")


@bot.event
async def on_member_join(member: discord.Member):
    await _orig_on_member_join(member)
    addon = get_addon_config(member.guild.id)
    now = time.time()
    dq = JOIN_TRACKER[member.guild.id]
    dq.append(now)
    if addon.get('raid_join_enabled'):
        window = int(addon.get('raid_join_window_sec') or 15)
        threshold = int(addon.get('raid_join_threshold') or 5)
        recent = [t for t in dq if now - t <= window]
        if len(recent) >= threshold:
            await send_modlog(member.guild, f"🚨 Alerte raid: {len(recent)} arrivées en {window}s sur **{member.guild.name}**")
    if addon.get('autorole_enabled') and addon.get('autorole_id'):
        role = member.guild.get_role(int(addon.get('autorole_id')))
        if role:
            try:
                await member.add_roles(role, reason='Autorole')
            except Exception:
                pass
    if addon.get('dm_welcome_enabled'):
        try:
            await member.send(f"👋 Bienvenue sur **{member.guild.name}** !")
        except Exception:
            pass


@bot.event
async def on_member_remove(member: discord.Member):
    await _orig_on_member_remove(member)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    await _orig_on_raw_reaction_add(payload)
    if payload.guild_id is None or bot.user is None or payload.user_id == bot.user.id:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    addon = get_addon_config(payload.guild_id)
    if addon.get('starboard_enabled') and str(payload.emoji) == '⭐':
        try:
            channel = guild.get_channel(payload.channel_id)
            star_ch_id = addon.get('starboard_channel_id')
            if not channel or not star_ch_id:
                return
            star_ch = guild.get_channel(int(star_ch_id))
            if not star_ch:
                return
            msg = await channel.fetch_message(payload.message_id)
            star_reaction = None
            for r in msg.reactions:
                if str(r.emoji) == '⭐':
                    star_reaction = r
                    break
            if not star_reaction:
                return
            threshold = int(addon.get('starboard_threshold') or 3)
            key = (payload.guild_id, payload.message_id)
            if star_reaction.count >= threshold and key not in STARBOARD_CACHE:
                emb = discord.Embed(description=msg.content or '(sans texte)', color=0xFFD166)
                emb.set_author(name=str(msg.author), icon_url=getattr(msg.author.display_avatar, 'url', None))
                emb.add_field(name='Origine', value=msg.jump_url, inline=False)
                if msg.attachments:
                    emb.set_image(url=msg.attachments[0].url)
                await star_ch.send(content=f"⭐ **{star_reaction.count}** dans {msg.channel.mention}", embed=emb)
                STARBOARD_CACHE.add(key)
        except Exception as e:
            add_log(f"starboard error: {e}")


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    await _orig_on_raw_reaction_remove(payload)


@bot.command()
async def snipe(ctx):
    data = LAST_DELETED.get((ctx.guild.id, ctx.channel.id)) if ctx.guild else None
    if not data:
        return await ctx.send('Rien à snipe.')
    emb = discord.Embed(title='🕵️ Snipe', description=data.get('content') or '(vide)', color=0x5AA7FF)
    emb.set_footer(text=f"Auteur: {data.get('author')} • ID {data.get('author_id')}")
    if data.get('attachments'):
        emb.add_field(name='Pièces jointes', value='\n'.join(data['attachments'][:3]), inline=False)
    await ctx.send(embed=emb)


@bot.command()
async def afk(ctx, *, reason: str = 'AFK'):
    if not ctx.guild:
        return await ctx.send('Serveur uniquement.')
    AFK_USERS[(ctx.guild.id, ctx.author.id)] = reason
    await ctx.send(f"💤 {ctx.author.mention} est maintenant AFK: {reason}")


@bot.command()
async def work(ctx):
    if not ctx.guild:
        return await ctx.send('Serveur uniquement.')
    cfg = get_guild_config(ctx.guild.id)
    if not cfg.get('economy_enabled', 1):
        return await ctx.send('Économie désactivée.')
    eco = economy_cfg(ctx.guild.id)
    gain = random.randint(eco['econ_work_min'], eco['econ_work_max'])
    row = econ_get(ctx.guild.id, ctx.author.id)
    econ_set(ctx.guild.id, ctx.author.id, int(row['balance']) + gain, int(row['last_daily_ts']))
    await ctx.send(f"🛠️ Travail terminé: +{gain} coins.")


@bot.command()
async def give(ctx, member: discord.Member, amount: int):
    await pay(ctx, member, amount)


@bot.command()
@commands.has_permissions(manage_guild=True)
async def gstart(ctx, duration: str, winners: int, *, prize: str):
    await giveaway(ctx, duration, winners, prize=prize)


@bot.command()
@commands.has_permissions(manage_guild=True)
async def greroll(ctx, message_id: int):
    try:
        msg = await ctx.channel.fetch_message(message_id)
        users = []
        for r in msg.reactions:
            if str(r.emoji) == '🎉':
                async for u in r.users():
                    if not u.bot:
                        users.append(u)
                break
        if not users:
            return await ctx.send('Aucun participant.')
        winner = random.choice(users)
        await ctx.send(f"🎉 Nouveau gagnant: {winner.mention}")
    except Exception as e:
        await ctx.send(f"Erreur reroll: {e}")


@bot.command()
async def closeticket(ctx):
    if not ctx.channel.name.startswith('ticket-'):
        return await ctx.send('Ce salon n’est pas un ticket.')
    addon = get_addon_config(ctx.guild.id)
    transcript_channel = None
    if addon.get('transcript_channel_id'):
        transcript_channel = ctx.guild.get_channel(int(addon['transcript_channel_id']))
    if transcript_channel:
        try:
            history_lines = []
            async for m in ctx.channel.history(limit=100, oldest_first=True):
                history_lines.append(f"[{m.created_at.strftime('%Y-%m-%d %H:%M:%S')}] {m.author}: {m.content}")
            text = '\n'.join(history_lines)[:190000] or '(vide)'
            payload = text.encode('utf-8')
            file = discord.File(io.BytesIO(payload), filename=f"transcript-{ctx.channel.name}.txt")
            await transcript_channel.send(f"📁 Transcript de {ctx.channel.name}", file=file)
        except Exception as e:
            add_log(f"transcript error: {e}")
    await ctx.send('Fermeture du ticket dans 3s…')
    await asyncio.sleep(3)
    await ctx.channel.delete()


@bot.command()
@commands.has_permissions(manage_guild=True)
async def ticketpanel(ctx):
    view = TicketOpenView()
    msg = await ctx.send('🎫 Clique sur le bouton pour ouvrir un ticket.', view=view)
    set_addon_config(ctx.guild.id, ticket_panel_channel_id=ctx.channel.id, ticket_panel_message_id=msg.id)
    await ctx.send('✅ Panel ticket envoyé.')


@bot.tree.command(name='workplus', description='Gagner des coins en travaillant')
async def slash_workplus(interaction: discord.Interaction):
    if not interaction.guild_id:
        return await interaction.response.send_message('Serveur uniquement.', ephemeral=True)
    cfg = get_guild_config(interaction.guild_id)
    if not cfg.get('economy_enabled', 1):
        return await interaction.response.send_message('Économie désactivée.', ephemeral=True)
    eco = economy_cfg(interaction.guild_id)
    gain = random.randint(eco['econ_work_min'], eco['econ_work_max'])
    row = econ_get(interaction.guild_id, interaction.user.id)
    econ_set(interaction.guild_id, interaction.user.id, int(row['balance']) + gain, int(row['last_daily_ts']))
    await interaction.response.send_message(f'🛠️ +{gain} coins', ephemeral=True)


@bot.tree.command(name='snipeplus', description='Voir le dernier message supprimé')
async def slash_snipeplus(interaction: discord.Interaction):
    if not interaction.guild_id or not interaction.channel_id:
        return await interaction.response.send_message('Serveur uniquement.', ephemeral=True)
    data = LAST_DELETED.get((interaction.guild_id, interaction.channel_id))
    if not data:
        return await interaction.response.send_message('Rien à snipe.', ephemeral=True)
    await interaction.response.send_message(f"🕵️ {data.get('author')}: {data.get('content') or '(vide)'}", ephemeral=True)


@bot.tree.command(name='afkplus', description='Passer en AFK')
async def slash_afkplus(interaction: discord.Interaction, reason: str = 'AFK'):
    if not interaction.guild_id:
        return await interaction.response.send_message('Serveur uniquement.', ephemeral=True)
    AFK_USERS[(interaction.guild_id, interaction.user.id)] = reason
    await interaction.response.send_message(f'💤 AFK activé: {reason}', ephemeral=True)


@bot.tree.command(name='serverstatsplus', description='Voir les stats du serveur')
async def slash_serverstatsplus(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message('Serveur uniquement.', ephemeral=True)
    g = interaction.guild
    await interaction.response.send_message(f"📊 **{g.name}**\nMembres: {g.member_count}\nSalons: {len(g.channels)}\nRôles: {len(g.roles)}", ephemeral=True)


@app.get('/api/healthz')
async def api_healthz():
    return {'ok': True, 'bot_connected': bool(bot.user), 'latency_ms': round(bot.latency * 1000) if bot.user else None, 'guilds': len(getattr(bot, 'guilds', []) or []), 'uptime_sec': int(time.time() - START_TIME)}


@app.post('/api/addons/get')
async def api_addons_get(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({'error': auth(data)}, status_code=403)
    gid = int(data.get('g') or 0)
    if gid <= 0:
        return JSONResponse({'error': 'Guild invalide'}, status_code=400)
    cfg = get_addon_config(gid)
    cfg['bad_words'] = badwords_list(gid)
    return cfg


@app.post('/api/addons/set')
async def api_addons_set(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({'error': auth(data)}, status_code=403)
    gid = int(data.get('g') or 0)
    if gid <= 0:
        return JSONResponse({'error': 'Guild invalide'}, status_code=400)
    set_addon_config(gid, anti_mention_spam=_bool(data.get('anti_mention_spam')), mention_threshold=int(as_int_or_none(data.get('mention_threshold')) or 5), anti_bad_words=_bool(data.get('anti_bad_words')), anti_duplicate=_bool(data.get('anti_duplicate')), anti_ghost_ping=_bool(data.get('anti_ghost_ping')), starboard_enabled=_bool(data.get('starboard_enabled')), starboard_channel_id=as_int_or_none(data.get('starboard_channel_id')), starboard_threshold=int(as_int_or_none(data.get('starboard_threshold')) or 3), snipe_enabled=_bool(data.get('snipe_enabled')), dm_welcome_enabled=_bool(data.get('dm_welcome_enabled')), autorole_enabled=_bool(data.get('autorole_enabled')), autorole_id=as_int_or_none(data.get('autorole_id')), suggest_autoreact=_bool(data.get('suggest_autoreact')), raid_join_enabled=_bool(data.get('raid_join_enabled')), raid_join_threshold=int(as_int_or_none(data.get('raid_join_threshold')) or 5), raid_join_window_sec=int(as_int_or_none(data.get('raid_join_window_sec')) or 15), econ_daily_min=int(as_int_or_none(data.get('econ_daily_min')) or 100), econ_daily_max=int(as_int_or_none(data.get('econ_daily_max')) or 200), econ_work_min=int(as_int_or_none(data.get('econ_work_min')) or 50), econ_work_max=int(as_int_or_none(data.get('econ_work_max')) or 120), transcript_channel_id=as_int_or_none(data.get('transcript_channel_id')))
    add_log(f'Panel: addons saved guild={gid}')
    return {'ok': True}


@app.post('/api/badwords/add')
async def api_badwords_add(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({'error': auth(data)}, status_code=403)
    gid = int(data.get('g') or 0)
    word = (data.get('word') or '').strip()
    if gid <= 0 or not word:
        return JSONResponse({'error': 'Paramètres invalides'}, status_code=400)
    badword_add(gid, word)
    return {'ok': True, 'items': badwords_list(gid)}


@app.post('/api/badwords/remove')
async def api_badwords_remove(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({'error': auth(data)}, status_code=403)
    gid = int(data.get('g') or 0)
    word = (data.get('word') or '').strip()
    if gid <= 0 or not word:
        return JSONResponse({'error': 'Paramètres invalides'}, status_code=400)
    badword_remove(gid, word)
    return {'ok': True, 'items': badwords_list(gid)}


@app.post('/api/stats/overview')
async def api_stats_overview(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({'error': auth(data)}, status_code=403)
    gid = int(data.get('g') or 0)
    guild = bot.get_guild(gid) if gid else None
    if not guild:
        return JSONResponse({'error': 'Serveur introuvable'}, status_code=404)
    humans = len([m for m in guild.members if not m.bot])
    bots = len([m for m in guild.members if m.bot])
    return {'name': guild.name, 'members': guild.member_count, 'humans': humans, 'bots': bots, 'roles': len(guild.roles), 'text_channels': len(guild.text_channels), 'voice_channels': len(guild.voice_channels), 'xp_top': xp_leaderboard(gid, limit=5), 'shop_items': len(shop_list(gid))}


@app.post('/api/economy/config/get')
async def api_economy_config_get(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({'error': auth(data)}, status_code=403)
    gid = int(data.get('g') or 0)
    if gid <= 0:
        return JSONResponse({'error': 'Guild invalide'}, status_code=400)
    return economy_cfg(gid)


@app.post('/api/economy/config/set')
async def api_economy_config_set(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({'error': auth(data)}, status_code=403)
    gid = int(data.get('g') or 0)
    if gid <= 0:
        return JSONResponse({'error': 'Guild invalide'}, status_code=400)
    set_addon_config(gid, econ_daily_min=int(as_int_or_none(data.get('econ_daily_min')) or 100), econ_daily_max=int(as_int_or_none(data.get('econ_daily_max')) or 200), econ_work_min=int(as_int_or_none(data.get('econ_work_min')) or 50), econ_work_max=int(as_int_or_none(data.get('econ_work_max')) or 120))
    return {'ok': True}


@app.post('/api/economy/user/get')
async def api_economy_user_get(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({'error': auth(data)}, status_code=403)
    gid = int(data.get('g') or 0)
    uid = int(data.get('u') or 0)
    if gid <= 0 or uid <= 0:
        return JSONResponse({'error': 'Paramètres invalides'}, status_code=400)
    return econ_get(gid, uid)


@app.post('/api/economy/user/set')
async def api_economy_user_set(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({'error': auth(data)}, status_code=403)
    gid = int(data.get('g') or 0)
    uid = int(data.get('u') or 0)
    balance = int(data.get('balance') or 0)
    if gid <= 0 or uid <= 0:
        return JSONResponse({'error': 'Paramètres invalides'}, status_code=400)
    row = econ_get(gid, uid)
    econ_set(gid, uid, balance, int(row['last_daily_ts']))
    return {'ok': True, 'balance': balance}


@app.post('/api/reactionroles/list')
async def api_rr_list(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({'error': auth(data)}, status_code=403)
    gid = int(data.get('g') or 0)
    if gid <= 0:
        return JSONResponse({'error': 'Guild invalide'}, status_code=400)
    con = db_connect(); cur = con.cursor(); cur.execute('SELECT * FROM reaction_roles WHERE guild_id=? ORDER BY message_id ASC', (gid,)); items = [dict(r) for r in cur.fetchall()]; con.close()
    return {'items': items}


@app.post('/api/reactionroles/add')
async def api_rr_add(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({'error': auth(data)}, status_code=403)
    gid = int(data.get('g') or 0); message_id = int(data.get('message_id') or 0); emoji = str(data.get('emoji') or '').strip(); role_id = int(data.get('role_id') or 0)
    if gid <= 0 or message_id <= 0 or role_id <= 0 or not emoji:
        return JSONResponse({'error': 'Paramètres invalides'}, status_code=400)
    rr_add(gid, message_id, emoji, role_id)
    return {'ok': True}


@app.post('/api/reactionroles/remove')
async def api_rr_remove(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({'error': auth(data)}, status_code=403)
    gid = int(data.get('g') or 0); message_id = int(data.get('message_id') or 0); emoji = str(data.get('emoji') or '').strip()
    if gid <= 0 or message_id <= 0 or not emoji:
        return JSONResponse({'error': 'Paramètres invalides'}, status_code=400)
    rr_remove(gid, message_id, emoji)
    return {'ok': True}


@app.post('/api/tickets/config/get')
async def api_tickets_config_get(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({'error': auth(data)}, status_code=403)
    gid = int(data.get('g') or 0)
    if gid <= 0:
        return JSONResponse({'error': 'Guild invalide'}, status_code=400)
    base = get_guild_config(gid); addon = get_addon_config(gid)
    return {'ticket_category_id': base.get('ticket_category_id'), 'ticket_panel_channel_id': addon.get('ticket_panel_channel_id'), 'ticket_panel_message_id': addon.get('ticket_panel_message_id'), 'transcript_channel_id': addon.get('transcript_channel_id')}


@app.post('/api/tickets/config/set')
async def api_tickets_config_set(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({'error': auth(data)}, status_code=403)
    gid = int(data.get('g') or 0)
    if gid <= 0:
        return JSONResponse({'error': 'Guild invalide'}, status_code=400)
    set_guild_config(gid, ticket_category_id=as_int_or_none(data.get('ticket_category_id')))
    set_addon_config(gid, transcript_channel_id=as_int_or_none(data.get('transcript_channel_id')))
    return {'ok': True}


@app.post('/api/tickets/panel/send')
async def api_tickets_panel_send(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({'error': auth(data)}, status_code=403)
    gid = int(data.get('g') or 0); channel_id = int(data.get('channel_id') or 0)
    guild = bot.get_guild(gid) if gid else None
    if not guild:
        return JSONResponse({'error': 'Serveur introuvable'}, status_code=404)
    ch = guild.get_channel(channel_id)
    if not ch:
        return JSONResponse({'error': 'Salon introuvable'}, status_code=404)
    msg = await ch.send('🎫 Clique sur le bouton pour ouvrir un ticket.', view=TicketOpenView())
    set_addon_config(gid, ticket_panel_channel_id=channel_id, ticket_panel_message_id=msg.id)
    return {'ok': True, 'message_id': msg.id}


def patch_panel_html():
    global PANEL_HTML
    if 'tab-addonsplus' in PANEL_HTML:
        return
    PANEL_HTML = PANEL_HTML.replace('<button data-tab="tab-logs">Logs</button>', '<button data-tab="tab-logs">Logs</button>\n      <button data-tab="tab-addonsplus">Addons+</button>\n      <button data-tab="tab-economyplus">Économie+</button>\n      <button data-tab="tab-reactionroles">Reaction Roles</button>\n      <button data-tab="tab-ticketsplus">Tickets+</button>\n      <button data-tab="tab-analytics">Analytics</button>')
    extra_sections = """
    <section id="tab-addonsplus" class="tab"><div class="grid"><div class="card"><div class="title">Automod+</div><label><input type="checkbox" id="anti_mention_spam"/> Anti mention spam</label><label>Seuil mentions</label><input id="mention_threshold" placeholder="5"/><label><input type="checkbox" id="anti_bad_words"/> Anti mots interdits</label><label><input type="checkbox" id="anti_duplicate"/> Anti messages dupliqués</label><label><input type="checkbox" id="anti_ghost_ping"/> Anti ghost ping</label><label><input type="checkbox" id="snipe_enabled"/> Snipe activé</label><label><input type="checkbox" id="raid_join_enabled"/> Détection raid joins</label><label>Seuil joins</label><input id="raid_join_threshold" placeholder="5"/><label>Fenêtre joins (sec)</label><input id="raid_join_window_sec" placeholder="15"/><div class="row" style="margin-top:12px"><button class="btn primary" onclick="saveAddons()">Sauvegarder</button></div></div><div class="card"><div class="title">Starboard / Autorole / Welcome DM</div><label><input type="checkbox" id="starboard_enabled"/> Starboard activé</label><label>Salon starboard ID</label><input id="starboard_channel_id" placeholder="ID salon"/><label>Seuil étoiles</label><input id="starboard_threshold" placeholder="3"/><label><input type="checkbox" id="autorole_enabled"/> Autorole activé</label><label>Autorole ID</label><input id="autorole_id" placeholder="ID rôle"/><label><input type="checkbox" id="dm_welcome_enabled"/> DM de bienvenue</label><label><input type="checkbox" id="suggest_autoreact"/> Suggestions auto-réactions</label><div class="hint" id="addonsMsg">—</div></div></div><div class="grid" style="margin-top:14px"><div class="card"><div class="title">Mots interdits</div><div class="row"><input id="badword_input" placeholder="mot interdit"/><button class="btn" onclick="addBadword()">Ajouter</button><button class="btn danger" onclick="removeBadword()">Supprimer</button></div><div class="console" id="badwordBox">—</div></div><div class="card"><div class="title">Commandes ajoutées</div><div class="hint">!snipe • !afk • !work • !give • !gstart • !greroll • !ticketpanel • !closeticket</div></div></div></section>
    <section id="tab-economyplus" class="tab"><div class="grid"><div class="card"><div class="title">Réglages économie</div><label>Daily min</label><input id="econ_daily_min" placeholder="100"/><label>Daily max</label><input id="econ_daily_max" placeholder="200"/><label>Work min</label><input id="econ_work_min" placeholder="50"/><label>Work max</label><input id="econ_work_max" placeholder="120"/><div class="row" style="margin-top:12px"><button class="btn primary" onclick="saveEconomyConfig()">Sauvegarder</button></div></div><div class="card"><div class="title">Gérer une balance</div><label>ID utilisateur</label><input id="eco_user_id" placeholder="123456"/><div class="row"><button class="btn" onclick="loadEcoUser()">Charger</button></div><label>Balance</label><input id="eco_balance" placeholder="0"/><div class="row" style="margin-top:12px"><button class="btn primary" onclick="saveEcoUser()">Enregistrer</button></div><div class="hint" id="ecoUserMsg">—</div></div></div></section>
    <section id="tab-reactionroles" class="tab"><div class="grid"><div class="card"><div class="title">Ajouter / supprimer</div><label>Message ID</label><input id="rr_message_id" placeholder="ID message"/><label>Emoji</label><input id="rr_emoji" placeholder="⭐"/><label>Role ID</label><input id="rr_role_id" placeholder="ID rôle"/><div class="row" style="margin-top:12px"><button class="btn" onclick="rrAddPanel()">Ajouter</button><button class="btn danger" onclick="rrRemovePanel()">Supprimer</button><button class="btn" onclick="rrListPanel()">Actualiser</button></div></div><div class="card"><div class="title">Liste</div><div class="console" id="rrBox">—</div></div></div></section>
    <section id="tab-ticketsplus" class="tab"><div class="grid"><div class="card"><div class="title">Configuration tickets</div><label>Catégorie ticket ID</label><input id="ticket_category_id_plus" placeholder="ID catégorie"/><label>Salon transcripts ID</label><input id="transcript_channel_id" placeholder="ID salon transcript"/><div class="row" style="margin-top:12px"><button class="btn primary" onclick="saveTicketsCfg()">Sauvegarder</button></div></div><div class="card"><div class="title">Envoyer le panel ticket</div><label>Salon cible ID</label><input id="ticket_panel_channel_id_send" placeholder="ID salon"/><div class="row" style="margin-top:12px"><button class="btn" onclick="sendTicketPanel()">Envoyer</button><button class="btn" onclick="loadTicketsCfg()">Actualiser</button></div><div class="hint" id="ticketsMsg">—</div></div></div></section>
    <section id="tab-analytics" class="tab"><div class="grid"><div class="card"><div class="title">Vue d’ensemble</div><div class="console" id="overviewBox">—</div><div class="row" style="margin-top:12px"><button class="btn" onclick="loadOverview()">Rafraîchir</button></div></div><div class="card"><div class="title">Health</div><div class="console" id="healthApiBox">—</div><div class="row" style="margin-top:12px"><button class="btn" onclick="loadHealthApi()">Health API</button></div></div></div></section>
    """
    PANEL_HTML = PANEL_HTML.replace('</main>', extra_sections + '\n  </main>')
    extra_js = """
async function loadAddons(){ const d = await api('/api/addons/get', {k:keyVal(), g:guildVal()}); if(d.error) return; const ids=['anti_mention_spam','anti_bad_words','anti_duplicate','anti_ghost_ping','starboard_enabled','snipe_enabled','dm_welcome_enabled','autorole_enabled','suggest_autoreact','raid_join_enabled']; ids.forEach(id=>{const el=document.getElementById(id); if(el) el.checked=!!d[id];}); ['mention_threshold','starboard_channel_id','starboard_threshold','autorole_id','raid_join_threshold','raid_join_window_sec','econ_daily_min','econ_daily_max','econ_work_min','econ_work_max','transcript_channel_id'].forEach(id=>{const el=document.getElementById(id); if(el) el.value=d[id]??'';}); logBox('badwordBox',(d.bad_words||[]).map(x=>`• ${escapeHtml(x)}`).join('<br/>')||'Aucun mot.'); }
async function saveAddons(){ const payload={k:keyVal(),g:guildVal(),anti_mention_spam:document.getElementById('anti_mention_spam').checked,mention_threshold:document.getElementById('mention_threshold').value,anti_bad_words:document.getElementById('anti_bad_words').checked,anti_duplicate:document.getElementById('anti_duplicate').checked,anti_ghost_ping:document.getElementById('anti_ghost_ping').checked,starboard_enabled:document.getElementById('starboard_enabled').checked,starboard_channel_id:document.getElementById('starboard_channel_id').value,starboard_threshold:document.getElementById('starboard_threshold').value,snipe_enabled:document.getElementById('snipe_enabled').checked,dm_welcome_enabled:document.getElementById('dm_welcome_enabled').checked,autorole_enabled:document.getElementById('autorole_enabled').checked,autorole_id:document.getElementById('autorole_id').value,suggest_autoreact:document.getElementById('suggest_autoreact').checked,raid_join_enabled:document.getElementById('raid_join_enabled').checked,raid_join_threshold:document.getElementById('raid_join_threshold').value,raid_join_window_sec:document.getElementById('raid_join_window_sec').value,econ_daily_min:document.getElementById('econ_daily_min').value,econ_daily_max:document.getElementById('econ_daily_max').value,econ_work_min:document.getElementById('econ_work_min').value,econ_work_max:document.getElementById('econ_work_max').value,transcript_channel_id:document.getElementById('transcript_channel_id').value}; const d=await api('/api/addons/set', payload); document.getElementById('addonsMsg').innerText=d.error?('Erreur: '+d.error):'Addons sauvegardés.'; }
async function addBadword(){ const word=document.getElementById('badword_input').value.trim(); const d=await api('/api/badwords/add',{k:keyVal(),g:guildVal(),word}); if(d.error) return alert(d.error); logBox('badwordBox',(d.items||[]).map(x=>`• ${escapeHtml(x)}`).join('<br/>')||'Aucun mot.'); }
async function removeBadword(){ const word=document.getElementById('badword_input').value.trim(); const d=await api('/api/badwords/remove',{k:keyVal(),g:guildVal(),word}); if(d.error) return alert(d.error); logBox('badwordBox',(d.items||[]).map(x=>`• ${escapeHtml(x)}`).join('<br/>')||'Aucun mot.'); }
async function loadOverview(){ const d=await api('/api/stats/overview',{k:keyVal(),g:guildVal()}); if(d.error) return alert(d.error); let lines=[]; lines.push(`Serveur: ${d.name}`); lines.push(`Membres: ${d.members} (humains ${d.humans} / bots ${d.bots})`); lines.push(`Salons texte: ${d.text_channels} | vocaux: ${d.voice_channels}`); lines.push(`Rôles: ${d.roles} | Shop: ${d.shop_items}`); if(Array.isArray(d.xp_top)){ lines.push('--- XP Top ---'); d.xp_top.forEach((x,i)=>lines.push(`${i+1}. ${x.user_id} — lvl ${x.level} (${x.xp} xp)`)); } logBox('overviewBox', lines.map(escapeHtml).join('<br/>')); }
async function loadHealthApi(){ const r=await fetch('/api/healthz'); const d=await r.json(); let lines=Object.keys(d).map(k=>`${k}: ${d[k]}`); logBox('healthApiBox', lines.map(escapeHtml).join('<br/>')); }
async function saveEconomyConfig(){ const payload={k:keyVal(),g:guildVal(),econ_daily_min:document.getElementById('econ_daily_min').value,econ_daily_max:document.getElementById('econ_daily_max').value,econ_work_min:document.getElementById('econ_work_min').value,econ_work_max:document.getElementById('econ_work_max').value}; const d=await api('/api/economy/config/set', payload); if(d.error) return alert(d.error); alert('Réglages économie sauvegardés.'); }
async function loadEcoUser(){ const uid=document.getElementById('eco_user_id').value.trim(); const d=await api('/api/economy/user/get',{k:keyVal(),g:guildVal(),u:uid}); if(d.error) return alert(d.error); document.getElementById('eco_balance').value=d.balance??0; document.getElementById('ecoUserMsg').innerText='Utilisateur chargé.'; }
async function saveEcoUser(){ const uid=document.getElementById('eco_user_id').value.trim(); const balance=document.getElementById('eco_balance').value.trim(); const d=await api('/api/economy/user/set',{k:keyVal(),g:guildVal(),u:uid,balance}); document.getElementById('ecoUserMsg').innerText=d.error?('Erreur: '+d.error):('Balance enregistrée: '+d.balance); }
async function rrListPanel(){ const d=await api('/api/reactionroles/list',{k:keyVal(),g:guildVal()}); if(d.error) return alert(d.error); const lines=(d.items||[]).map(x=>`msg ${x.message_id} | ${x.emoji} -> ${x.role_id}`); logBox('rrBox', lines.map(escapeHtml).join('<br/>')||'Aucun.'); }
async function rrAddPanel(){ const payload={k:keyVal(),g:guildVal(),message_id:document.getElementById('rr_message_id').value,emoji:document.getElementById('rr_emoji').value,role_id:document.getElementById('rr_role_id').value}; const d=await api('/api/reactionroles/add',payload); if(d.error) return alert(d.error); rrListPanel(); }
async function rrRemovePanel(){ const payload={k:keyVal(),g:guildVal(),message_id:document.getElementById('rr_message_id').value,emoji:document.getElementById('rr_emoji').value}; const d=await api('/api/reactionroles/remove',payload); if(d.error) return alert(d.error); rrListPanel(); }
async function loadTicketsCfg(){ const d=await api('/api/tickets/config/get',{k:keyVal(),g:guildVal()}); if(d.error) return alert(d.error); document.getElementById('ticket_category_id_plus').value=d.ticket_category_id??''; document.getElementById('transcript_channel_id').value=d.transcript_channel_id??''; document.getElementById('ticketsMsg').innerText='Config tickets chargée.'; }
async function saveTicketsCfg(){ const payload={k:keyVal(),g:guildVal(),ticket_category_id:document.getElementById('ticket_category_id_plus').value,transcript_channel_id:document.getElementById('transcript_channel_id').value}; const d=await api('/api/tickets/config/set',payload); document.getElementById('ticketsMsg').innerText=d.error?('Erreur: '+d.error):'Config tickets sauvegardée.'; }
async function sendTicketPanel(){ const payload={k:keyVal(),g:guildVal(),channel_id:document.getElementById('ticket_panel_channel_id_send').value}; const d=await api('/api/tickets/panel/send',payload); document.getElementById('ticketsMsg').innerText=d.error?('Erreur: '+d.error):('Panel ticket envoyé. message='+d.message_id); }
const _oldLoadAll=loadAll; loadAll=async function(){ await _oldLoadAll(); await loadAddons(); await loadOverview(); await loadHealthApi(); await rrListPanel(); await loadTicketsCfg(); };
"""
    PANEL_HTML = PANEL_HTML.replace('</script>', extra_js + '\n</script>')

patch_panel_html()

async def main():
    db_init()
    db_init_plus()
    asyncio.create_task(start_bot_safely())
    config = uvicorn.Config(app, host='0.0.0.0', port=PORT, log_level='info')
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == '__main__':
    asyncio.run(main())
