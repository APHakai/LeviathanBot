import os
import asyncio
import datetime
import discord
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from discord.ext import commands

# --- CONFIGURATION ---
PORT = int(os.environ.get("PORT", 10000))
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
ADMIN_KEY = "LEVIATHAN_2026"

# --- BOT SETUP ---
intents = discord.Intents.all() # Obligatoire pour voir les salons
bot = commands.Bot(command_prefix="!", intents=intents)
action_logs = []

def add_log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    # On garde les logs en mémoire pour le site
    action_logs.append(f"[{ts}] {msg}")
    if len(action_logs) > 50: action_logs.pop(0)
    print(msg) # Affiche aussi dans la console Render

# --- FONCTION LOCKDOWN BAVARDE ---
async def run_lockdown(guild_id):
    guild = bot.get_guild(guild_id)
    if not guild:
        add_log("❌ ERREUR : Bot ne trouve pas le serveur (ID invalide ?)")
        return

    add_log(f"🔄 Démarrage Lockdown sur : {guild.name}")
    
    # 1. Vérification Admin
    if not guild.me.guild_permissions.administrator:
        add_log("❌ ERREUR CRITIQUE : Le bot n'est pas ADMINISTRATEUR !")
        return

    # 2. Vérification Hiérarchie
    add_log(f"ℹ️ Mon rôle le plus haut : {guild.me.top_role.name} (Position: {guild.me.top_role.position})")

    count = 0
    errors = 0
    
    # 3. Action
    for channel in guild.text_channels:
        try:
            # Vérifie si le bot peut voir le salon
            if not channel.permissions_for(guild.me).manage_channels:
                add_log(f"⚠️ Pas de perm sur #{channel.name}")
                errors += 1
                continue

            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.send_messages = False
            await channel.set_permissions(guild.default_role, overwrite=overwrite)
            count += 1
        except Exception as e:
            add_log(f"💥 Crash sur #{channel.name} : {e}")
            errors += 1
            
    add_log(f"✅ FINI : {count} salons verrouillés | {errors} échecs.")

async def run_unlock(guild_id):
    guild = bot.get_guild(guild_id)
    if not guild: return
    add_log(f"🔓 Unlock demandé sur {guild.name}")
    for channel in guild.text_channels:
        try:
            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.send_messages = True
            await channel.set_permissions(guild.default_role, overwrite=overwrite)
        except: continue
    add_log("🔓 Unlock terminé.")

# --- SITE WEB ---
app = FastAPI()

@app.get("/leviathan", response_class=HTMLResponse)
async def panel():
    if not bot.guilds:
        options = "<option>Aucun serveur trouvé (Bot non connecté ?)</option>"
    else:
        options = "".join([f"<option value='{g.id}'>{g.name}</option>" for g in bot.guilds])
        
    return f"""
<!DOCTYPE html>
<html>
<head><title>DEBUG MODE</title>
<style>
    body {{ background:#111; color:#0f0; font-family:monospace; padding:20px; }}
    .box {{ border:1px solid #0f0; padding:10px; margin:10px 0; }}
    button {{ padding:10px; width:100%; background:#222; color:#fff; cursor:pointer; }}
    .logs {{ height:300px; overflow-y:scroll; background:#000; border:1px solid #555; padding:5px; }}
</style></head>
<body>
    <h1>🛠️ PANEL DE DIAGNOSTIC</h1>
    <div class="box">
        <select id="g" style="width:100%; padding:10px;">{options}</select>
        <br><br>
        <input type="password" id="k" placeholder="Mot de passe" style="width:100%">
    </div>
    <button onclick="run('lockdown')">🔴 TEST LOCKDOWN</button>
    <button onclick="run('unlock')">🟢 UNLOCK</button>
    
    <h3>LOGS DU BOT :</h3>
    <div class="logs" id="l">En attente...</div>

    <script>
        async function run(a){{
            const g = document.getElementById('g').value;
            const k = document.getElementById('k').value;
            await fetch('/api/run', {{
                method:'POST',
                body: JSON.stringify({{action:a, g:g, k:k}})
            }});
        }}
        setInterval(async ()=>{{
            let r = await fetch('/api/logs');
            let j = await r.json();
            document.getElementById('l').innerHTML = j.slice().reverse().join('<br>');
        }}, 1000);
    </script>
</body></html>
"""

@app.post("/api/run")
async def api_run(request: Request):
    data = await request.json()
    if data.get('k') != ADMIN_KEY: return {"error": "Mauvais MDP"}
    
    # On lance la fonction en tâche de fond
    if data['action'] == 'lockdown':
        asyncio.create_task(run_lockdown(int(data['g'])))
    elif data['action'] == 'unlock':
        asyncio.create_task(run_unlock(int(data['g'])))
    return {"status": "ok"}

@app.get("/api/logs")
async def get_logs(): return action_logs

# --- LANCEMENT ---
async def main():
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT)
    server = uvicorn.Server(config)
    await asyncio.gather(bot.start(DISCORD_TOKEN), server.serve())

if __name__ == "__main__":
    asyncio.run(main())