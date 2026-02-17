import os
import asyncio
import datetime
import discord
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from discord.ext import commands

# --- CONFIGURATION ---
# Render donne le port automatiquement, sinon 10000
PORT = int(os.environ.get("PORT", 10000))
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "") # Le token sera lu depuis les variables Render
ADMIN_KEY = "LEVIATHAN_2026" # Mot de passe du panel

# --- BOT CONFIGURATION ---
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
action_logs = []

def add_log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    action_logs.append(f"[{ts}] {msg}")
    if len(action_logs) > 50: action_logs.pop(0)

# --- COMMANDES DE SÉCURITÉ ---
async def run_lockdown(guild):
    add_log(f"🔒 LOCKDOWN : Verrouillage de {guild.name}...")
    role = guild.default_role
    for channel in guild.text_channels:
        try:
            overwrite = channel.overwrites_for(role)
            overwrite.send_messages = False
            await channel.set_permissions(role, overwrite=overwrite)
        except: continue
    add_log("🔒 Fin du verrouillage.")

async def run_unlock(guild):
    add_log(f"🔓 UNLOCK : Déverrouillage de {guild.name}...")
    role = guild.default_role
    for channel in guild.text_channels:
        try:
            overwrite = channel.overwrites_for(role)
            overwrite.send_messages = True
            await channel.set_permissions(role, overwrite=overwrite)
        except: continue
    add_log("🔓 Fin du déverrouillage.")

# --- PANEL WEB (FASTAPI) ---
app = FastAPI()

@app.get("/")
async def home():
    return "Le bot est en ligne. Va sur /leviathan pour le panel."

@app.get("/leviathan", response_class=HTMLResponse)
async def panel():
    guild_options = "".join([f"<option value='{g.id}'>{g.name}</option>" for g in bot.guilds])
    return f"""
<!DOCTYPE html>
<html>
<head><title>LEVIATHAN SECURITY</title>
<style>
    body {{ background:#050505; color:#0f0; font-family:monospace; padding:20px; }}
    .card {{ border:1px solid #0f0; padding:15px; background:#000; margin-bottom:15px; }}
    button {{ width:100%; padding:12px; margin:5px 0; background:#111; color:#0f0; border:1px solid #0f0; cursor:pointer; }}
    button:hover {{ background:#0f0; color:#000; }}
    input, select {{ width:100%; padding:12px; margin:5px 0; background:#111; color:#0f0; border:1px solid #0f0; }}
    .log-box {{ background:#000; height:300px; overflow-y:scroll; border:1px solid #333; padding:10px; }}
</style></head>
<body>
    <h1>🛡️ LEVIATHAN CONTROL</h1>
    <div class="card">
        <input type="password" id="k" placeholder="Mot de passe (LEVIATHAN_2026)">
        <select id="g">{guild_options}</select>
    </div>
    <div class="card">
        <h3>URGENCE</h3>
        <button onclick="exe('lockdown')">🔒 LOCKDOWN (Verrouiller tout)</button>
        <button onclick="exe('unlock')">🔓 UNLOCK (Ouvrir tout)</button>
    </div>
    <div class="card">
        <h3>LOGS LIVE</h3>
        <div class="log-box" id="logs">Chargement...</div>
    </div>
    <script>
        async function exe(a){{
            const d = {{k:document.getElementById('k').value, g:document.getElementById('g').value, action:a}};
            await fetch('/api/run', {{method:'POST', body:JSON.stringify(d)}});
        }}
        setInterval(async () => {{
            const r = await fetch('/api/logs');
            const d = await r.json();
            document.getElementById('logs').innerHTML = d.slice().reverse().join('<br>');
        }}, 2000);
    </script>
</body></html>"""

@app.post("/api/run")
async def api_run(request: Request):
    try:
        d = await request.json()
        if d.get('k') != ADMIN_KEY: return JSONResponse({"error":"Mauvais mot de passe"}, status_code=403)
        guild = bot.get_guild(int(d['g']))
        if d['action'] == 'lockdown': asyncio.create_task(run_lockdown(guild))
        elif d['action'] == 'unlock': asyncio.create_task(run_unlock(guild))
        return {"status": "ok"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/logs")
async def get_logs(): return action_logs

@bot.event
async def on_ready():
    add_log(f"Bot connecté : {bot.user}")

# --- LANCEMENT ---
async def main():
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT)
    server = uvicorn.Server(config)
    # Lance le bot ET le site web en même temps
    await asyncio.gather(bot.start(DISCORD_TOKEN), server.serve())

if __name__ == "__main__":
    asyncio.run(main())