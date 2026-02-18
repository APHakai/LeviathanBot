import os
import asyncio
import datetime
import discord
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from discord.ext import commands
from discord import app_commands

# --- CONFIGURATION ---
PORT = int(os.environ.get("PORT", 10000))
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "") # Ton token sur Render
ADMIN_KEY = "LEVIATHAN_2026" # Ton mot de passe Panel

# --- BOT SETUP ---
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
action_logs = []

def add_log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{ts}] {msg}"
    action_logs.append(log_entry)
    if len(action_logs) > 100: action_logs.pop(0)
    print(log_entry)

# --- FONCTIONS MODÉRATION ---

async def run_lockdown(guild_id, status):
    guild = bot.get_guild(guild_id)
    if not guild: return "Serveur introuvable"
    
    add_log(f"🔒 LOCKDOWN {'ACTIVÉ' if status else 'DÉSACTIVÉ'} sur {guild.name}")
    role = guild.default_role
    count = 0
    
    for channel in guild.text_channels:
        try:
            overwrite = channel.overwrites_for(role)
            overwrite.send_messages = not status # False = Lock, True = Unlock
            await channel.set_permissions(role, overwrite=overwrite)
            count += 1
        except: continue
    return f"Action effectuée sur {count} salons."

async def run_purge(guild_id, amount):
    guild = bot.get_guild(guild_id)
    count = 0
    for channel in guild.text_channels:
        try:
            deleted = await channel.purge(limit=amount)
            count += len(deleted)
            add_log(f"🧹 Purge de {len(deleted)} msgs dans #{channel.name}")
        except: continue
    return f"{count} messages supprimés au total."

async def run_punishment(guild_id, user_id, action, reason):
    guild = bot.get_guild(guild_id)
    if not guild: return "Serveur introuvable"
    
    try:
        member = await guild.fetch_member(user_id)
    except:
        return "Membre introuvable (Vérifie l'ID)"

    try:
        if action == "kick":
            await member.kick(reason=reason)
            add_log(f"👢 KICK: {member.name} a été expulsé.")
            return f"{member.name} expulsé."
            
        elif action == "ban":
            await member.ban(reason=reason)
            add_log(f"🔨 BAN: {member.name} a été banni.")
            return f"{member.name} banni."
            
        elif action == "timeout":
            # Timeout de 1 heure
            duration = datetime.timedelta(hours=1)
            await member.timeout(duration, reason=reason)
            add_log(f"guo MUTE: {member.name} réduit au silence (1h).")
            return f"{member.name} mute pour 1h."
            
    except Exception as e:
        return f"Erreur : {str(e)}"

# --- DASHBOARD WEB ---
app = FastAPI()

@app.get("/leviathan", response_class=HTMLResponse)
async def panel():
    if not bot.guilds:
        guild_opts = "<option>Aucun serveur (Bot Offline?)</option>"
    else:
        guild_opts = "".join([f"<option value='{g.id}'>{g.name}</option>" for g in bot.guilds])

    return f"""
<!DOCTYPE html>
<html lang="fr">
<head>
    <title>LEVIATHAN /// ADMIN PANEL</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        :root {{ --bg: #0a0a0a; --panel: #111; --border: #333; --accent: #00ff41; --danger: #ff003c; --text: #eee; }}
        body {{ background: var(--bg); color: var(--text); font-family: 'Courier New', monospace; margin: 0; padding: 20px; }}
        
        /* HEADER */
        .header {{ display: flex; justify-content: space-between; border-bottom: 2px solid var(--accent); padding-bottom: 10px; margin-bottom: 20px; }}
        h1 {{ margin: 0; text-transform: uppercase; letter-spacing: 2px; text-shadow: 0 0 10px var(--accent); }}
        
        /* LAYOUT */
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }}
        .card {{ background: var(--panel); border: 1px solid var(--border); padding: 20px; box-shadow: 0 0 15px rgba(0,0,0,0.5); }}
        .card h3 {{ border-left: 4px solid var(--accent); padding-left: 10px; margin-top: 0; color: var(--accent); }}
        .danger h3 {{ border-color: var(--danger); color: var(--danger); }}

        /* CONTROLS */
        input, select {{ width: 100%; padding: 10px; background: #000; border: 1px solid #555; color: white; margin-bottom: 10px; font-family: inherit; }}
        
        button {{ width: 100%; padding: 12px; margin-top: 5px; border: none; font-weight: bold; cursor: pointer; transition: 0.3s; text-transform: uppercase; }}
        
        .btn-safe {{ background: #222; color: var(--accent); border: 1px solid var(--accent); }}
        .btn-safe:hover {{ background: var(--accent); color: #000; box-shadow: 0 0 10px var(--accent); }}
        
        .btn-danger {{ background: #220000; color: var(--danger); border: 1px solid var(--danger); }}
        .btn-danger:hover {{ background: var(--danger); color: white; box-shadow: 0 0 10px var(--danger); }}

        /* LOGS */
        .console {{ background: #000; border: 1px solid #333; height: 300px; overflow-y: auto; padding: 10px; font-size: 12px; color: #ccc; }}
        .log-entry {{ border-bottom: 1px solid #222; padding: 2px 0; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🛡️ LEVIATHAN SYSTEM</h1>
        <div style="color: var(--accent)">STATUS: EN LIGNE</div>
    </div>

    <div class="card" style="margin-bottom: 20px;">
        <h3>🔑 ACCÈS & CIBLE</h3>
        <div style="display:flex; gap:10px;">
            <input type="password" id="key" placeholder="MOT DE PASSE ADMIN">
            <select id="guild">{guild_opts}</select>
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <h3>🔒 SÉCURITÉ GLOBALE</h3>
            <p>Contrôle d'urgence des salons.</p>
            <button class="btn-safe" onclick="api('lockdown', true)">🔒 VERROUILLER TOUT (Lockdown)</button>
            <button class="btn-safe" onclick="api('lockdown', false)">🔓 DÉVERROUILLER TOUT (Unlock)</button>
            <br><br>
            <button class="btn-danger" onclick="api('purge', 10)">🧹 PURGE RAPIDE (10 msg)</button>
            <button class="btn-danger" onclick="api('purge', 50)">🧹 PURGE MASSIVE (50 msg)</button>
        </div>

        <div class="card danger">
            <h3>💀 CIBLER UN UTILISATEUR</h3>
            <input type="text" id="target_id" placeholder="ID UTILISATEUR (ex: 837482...)">
            <input type="text" id="reason" placeholder="RAISON (Optionnel)">
            <div style="display:grid; grid-template-columns: 1fr 1fr; gap:5px;">
                <button class="btn-safe" onclick="api('timeout')">🤐 MUTE (1H)</button>
                <button class="btn-danger" onclick="api('kick')">👢 KICK</button>
            </div>
            <button class="btn-danger" style="margin-top:5px; background:red; color:white" onclick="api('ban')">🔨 BANNIR DÉFINITIVEMENT</button>
        </div>
    </div>

    <div class="card" style="margin-top: 20px;">
        <h3>📡 LIVE LOGS</h3>
        <div class="console" id="logs">Initialisation du flux de données...</div>
    </div>

    <script>
        async function api(action, val=null) {{
            const key = document.getElementById('key').value;
            const guild = document.getElementById('guild').value;
            const target = document.getElementById('target_id').value;
            const reason = document.getElementById('reason').value;

            // Feedback visuel
            document.getElementById('logs').innerHTML += `<div class="log-entry" style="color:yellow">> Commande envoyée: ${action}...</div>`;

            const payload = {{ k: key, g: guild, action: action, val: val, target: target, reason: reason }};
            
            try {{
                const req = await fetch('/api/run', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify(payload)
                }});
                const res = await req.json();
                if(res.error) alert("ERREUR: " + res.error);
            }} catch (e) {{ alert("Erreur de connexion"); }}
        }}

        // Auto-refresh Logs
        setInterval(async () => {{
            try {{
                const r = await fetch('/api/logs');
                const logs = await r.json();
                const html = logs.slice().reverse().map(l => `<div class="log-entry">${{l}}</div>`).join('');
                document.getElementById('logs').innerHTML = html;
            }} catch (e) {{}}
        }}, 2000);
    </script>
</body>
</html>
"""

@app.post("/api/run")
async def api_run(request: Request):
    data = await request.json()
    
    # Sécurité
    if data.get('k') != ADMIN_KEY: 
        return JSONResponse({"error": "MOT DE PASSE INCORRECT"}, status_code=403)
    
    gid = int(data['g'])
    action = data['action']
    
    result = "Aucune action"
    
    # Dispatcher
    if action == 'lockdown':
        result = await run_lockdown(gid, data['val'])
    elif action == 'purge':
        result = await run_purge(gid, data['val']) # val = nombre de messages
    elif action in ['ban', 'kick', 'timeout']:
        if not data.get('target'): return {"error": "ID Utilisateur manquant"}
        result = await run_punishment(gid, int(data['target']), action, data.get('reason', 'Aucune raison'))

    return {"status": "OK", "details": result}

@app.get("/api/logs")
async def get_logs(): return action_logs

# --- START ---
async def main():
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT)
    server = uvicorn.Server(config)
    await asyncio.gather(bot.start(DISCORD_TOKEN), server.serve())

if __name__ == "__main__":
    asyncio.run(main())