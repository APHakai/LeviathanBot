import os
import re
import time
import json
import math
import asyncio
import random
import sqlite3
import datetime
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
    """
    Convertit une durée humaine en secondes.

    Formats acceptés (insensible à la casse, espaces tolérés):
      - Format simple: 10s, 15m, 2h, 1d, 3w, 2mo, 1y
      - Format composé: 1h30m, 2d6h, 1w2d3h10m, etc.
      - Alias:
          m / min / mins / minute(s)  -> minutes
          h / hr / hrs / heure(s)     -> heures
          d / day(s) / jour(s)        -> jours
          w / week(s) / semaine(s)    -> semaines (7 jours)
          mo / month(s) / mois        -> mois (30 jours)
          y / yr / year(s) / an(s)    -> années (365 jours)

    Notes de précision:
      - 1 mois = 30 jours (approximatif mais stable pour les timeouts Discord).
      - 1 an   = 365 jours.
    """
    s = (s or "").strip().lower()
    if not s:
        return None

    # Normalisation rapide des alias longs (sans casser "mo")
    s = s.replace("minutes", "min").replace("minute", "min")
    s = s.replace("heures", "h").replace("heure", "h").replace("hrs", "h").replace("hr", "h")
    s = s.replace("jours", "d").replace("jour", "d").replace("days", "d").replace("day", "d")
    s = s.replace("semaines", "w").replace("semaine", "w").replace("weeks", "w").replace("week", "w")
    s = s.replace("months", "mo").replace("month", "mo").replace("mois", "mo")
    s = s.replace("years", "y").replace("year", "y").replace("yrs", "y").replace("yr", "y")
    s = s.replace("ans", "y").replace("an", "y")

    # Enlève les espaces pour accepter "1 h 30 m"
    s = re.sub(r"\s+", "", s)

    token_re = re.compile(r"(\d+)(s|min|m|h|d|w|mo|y)")
    parts = token_re.findall(s)
    if not parts:
        return None

    # Vérifie qu'on a parsé toute la chaîne (ex: "10mabc" -> invalide)
    joined = "".join([f"{n}{u}" for n, u in parts])
    if joined != s:
        return None

    unit_mult = {
        "s": 1,
        "m": 60,
        "min": 60,
        "h": 3600,
        "d": 86400,
        "w": 7 * 86400,
        "mo": 30 * 86400,
        "y": 365 * 86400,
    }

    total = 0
    for n_str, unit in parts:
        n = int(n_str)
        total += n * unit_mult[unit]
    return total if total > 0 else None


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
            <div>
              <label>Membre (menu déroulant)</label>
              <input id="memberSearch" placeholder="Chercher pseudo (ex: levi)..." oninput="filterMembers()" />
              <select id="target_select"></select>
              <div class="hint">Astuce: commence à taper pour filtrer. Si la liste est vide, clique “Charger”.</div>
            </div>

            <div>
              <label>Ou ID utilisateur (fallback)</label>
              <input id="target" placeholder="ex: 1234567890"/>
              <label>Raison</label>
              <input id="reason" placeholder="optionnel"/>
            </div>
          </div>

          <div class="row" style="margin-top:10px">
            <div>
              <label>Timeout (durée)</label>
              <select id="timeout_preset" onchange="onTimeoutPreset()">
                <option value="1m">1 minute</option>
                <option value="10m">10 minutes</option>
                <option value="30m">30 minutes</option>
                <option value="1h" selected>1 heure</option>
                <option value="6h">6 heures</option>
                <option value="12h">12 heures</option>
                <option value="1d">1 jour</option>
                <option value="3d">3 jours</option>
                <option value="1w">1 semaine</option>
                <option value="2w">2 semaines</option>
                <option value="1mo">1 mois</option>
                <option value="3mo">3 mois</option>
                <option value="1y">1 an</option>
                <option value="custom">Custom…</option>
              </select>
              <input id="timeout_custom" placeholder="ex: 90m, 2h30m, 1w2d" disabled />
              <div class="hint">
                Formats: <code>10m</code>, <code>2h</code>, <code>1d</code>, <code>1w</code>, <code>1mo</code>, <code>1y</code>, ou composé <code>1h30m</code>.
                <br/>⚠️ Discord limite le timeout à ~28 jours.
              </div>
            </div>
          </div>

          <div class="row" style="margin-top:10px">
            <button class="btn" onclick="panelPunish('timeout')">Timeout</button>
            <button class="btn" onclick="panelPunish('untimeout')">Un-timeout</button>
            <button class="btn" onclick="panelPunish('warn')">Warn</button>
            <button class="btn danger" onclick="panelPunish('kick')">Kick</button>
            <button class="btn danger" onclick="panelPunish('ban')">Ban</button>
          </div>
        </div></div>

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
  await loadMembers();
  await loadShop();
  await loadLogsOnce();
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
  // cible: menu déroulant > ID fallback
  const sel = (document.getElementById('target_select') || {}).value || '';
  const raw = (document.getElementById('target') || {}).value || '';
  const target = (sel && sel !== '0') ? sel : raw.trim();

  const reason = (document.getElementById('reason') || {}).value.trim() || 'Via Panel';

  const payload = {k:keyVal(), g:guildVal(), action:action, target:target, reason:reason};

  if(action === 'timeout'){
    const preset = (document.getElementById('timeout_preset') || {}).value || '1h';
    const custom = (document.getElementById('timeout_custom') || {}).value.trim();
    payload.duration = (preset === 'custom') ? custom : preset;
  }

  const d = await api('/api/run', payload);
  if(d.error) alert(d.error);
  else if(d.details) alert(d.details);
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

let __members = []; // [{id, name, display}]

async function loadMembers(){
  const g = guildVal();
  if(!g || g === '0') return;
  const d = await api('/api/members/list', {k:keyVal(), g:g});
  if(d.error) return;
  __members = Array.isArray(d.items) ? d.items : [];
  renderMembers(__members);
}

function renderMembers(items){
  const sel = document.getElementById('target_select');
  if(!sel) return;
  const prev = sel.value;
  sel.innerHTML = '';
  const opt0 = document.createElement('option');
  opt0.value = '0';
  opt0.textContent = '— sélectionner un membre —';
  sel.appendChild(opt0);

  (items || []).forEach(m => {
    const o = document.createElement('option');
    o.value = String(m.id);
    o.textContent = m.display || m.name || String(m.id);
    sel.appendChild(o);
  });

  if(prev) sel.value = prev;
}

function filterMembers(){
  const q = (document.getElementById('memberSearch') || {}).value || '';
  const needle = q.trim().toLowerCase();
  if(!needle){
    return renderMembers(__members);
  }
  // filtre rapide (pseudo + display)
  const out = (__members || []).filter(m => {
    const a = String(m.name || '').toLowerCase();
    const b = String(m.display || '').toLowerCase();
    return a.includes(needle) || b.includes(needle) || String(m.id).includes(needle);
  }).slice(0, 200); // évite de rendre 10k options
  renderMembers(out);
}

function onTimeoutPreset(){
  const preset = (document.getElementById('timeout_preset') || {}).value || '1h';
  const inp = document.getElementById('timeout_custom');
  if(!inp) return;
  inp.disabled = (preset !== 'custom');
  if(preset !== 'custom') inp.value = '';
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

@app.post("/api/members/list")
async def api_members_list(request: Request):
    data = await request.json()
    if auth(data):
        return JSONResponse({"error": auth(data)}, status_code=403)

    gid = int(data.get("g") or 0)
    guild = bot.get_guild(gid) if gid else None
    if not guild:
        return JSONResponse({"error": "Serveur introuvable (bot offline ?)"} , status_code=400)

    # On s'appuie sur le cache (chunk) pour éviter de spammer l'API Discord
    members = [m for m in (guild.members or []) if not m.bot]

    # Tri stable: display_name puis username
    def key(m):
        return (str(m.display_name or "").lower(), str(m.name or "").lower(), m.id)

    members.sort(key=key)

    # Limite hard pour rester léger côté panel
    members = members[:2000]

    items = []
    for m in members:
        disp = str(m.display_name)
        # Si surnom != username, on affiche les 2
        if m.nick and m.nick != m.name:
            display = f"{m.nick} (@{m.name})"
        else:
            display = f"{disp} (@{m.name})" if disp != m.name else disp
        items.append({"id": m.id, "name": m.name, "display": display})

    return {"items": items}

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

        if action in ("kick", "ban", "timeout", "untimeout", "warn"):
            target = str(data.get("target") or "").strip()
            reason = str(data.get("reason") or "Via Panel").strip() or "Via Panel"
            if not target.isdigit():
                return {"error": "Utilisateur invalide. Sélectionne un membre dans la liste ou mets un ID numérique."}
            uid = int(target)

            try:
                member = await guild.fetch_member(uid)
            except:
                return {"error": "Membre introuvable (ID incorrect ou pas dans le serveur)."}

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
                await send_modlog(guild, f"⚠️ Warn: {member.mention} | {reason} (via panel)")
                # DM best-effort
                try:
                    await member.send(
                        f"⚠️ Tu as reçu un avertissement sur **{guild.name}**.\n"
                        f"Raison: {reason}"
                    )
                except:
                    pass
                return {"details": "Warn OK."}

            if action == "untimeout":
                await member.timeout(None, reason=reason)
                add_infraction(gid, uid, None, "untimeout", reason)
                await send_modlog(guild, f"🔈 Un-timeout: {member.mention} | {reason} (via panel)")
                return {"details": "Un-timeout OK."}

            if action == "timeout":
                duration_raw = str(data.get("duration") or "1h").strip()
                sec = parse_duration_to_seconds(duration_raw)
                if not sec:
                    return {"error": "Durée invalide. Exemples: 10m, 2h, 1d, 1w, 1mo, 1y, ou 1h30m."}

                # Discord: timeout max ≈ 28 jours
                max_sec = 28 * 24 * 3600
                if sec > max_sec:
                    return {"error": "Durée trop longue. Discord limite le timeout à ~28 jours."}

                await member.timeout(datetime.timedelta(seconds=sec), reason=reason)
                add_infraction(gid, uid, None, "timeout", f"{duration_raw} | {reason}")
                await send_modlog(guild, f"🤐 Timeout: {member.mention} {duration_raw} | {reason} (via panel)")
                return {"details": f"Timeout OK ({duration_raw})."}

        return {"error": "Action inconnue."}
    except Exception as e:
        add_log(f"Panel run error: {e}")
        return {"error": f"Erreur: {e}"}

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

    # Pré-charge les membres pour alimenter le menu déroulant du panel (/api/members/list)
    try:
        for g in bot.guilds:
            try:
                await g.chunk(cache=True)
            except:
                continue
        add_log("Guild chunk (members) ✅")
    except Exception as e:
        add_log(f"Guild chunk error: {e}")
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

if __name__ == "__main__":
    asyncio.run(main())
