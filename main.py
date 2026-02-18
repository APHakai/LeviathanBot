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
    
    role = guild.default_role
    count = 0
    for channel in guild.text_channels:
        try:
            overwrite = channel.overwrites_for(role)
            overwrite.send_messages = not status
            await channel.set_permissions(role, overwrite=overwrite)
            count += 1
        except: continue
    
    state = "VERROUILLÉ" if status else "DÉVERROUILLÉ"
    add_log(f"LOCKDOWN: Serveur {state} ({count} salons).")
    return f"Succès : {count} salons modifiés."

async def run_purge(guild_id, amount):
    guild = bot.get_guild(guild_id)
    if not guild: return "Serveur introuvable"
    count = 0
    for channel in guild.text_channels:
        try:
            deleted = await channel.purge(limit=amount)
            count += len(deleted)
        except: continue
    add_log(f"PURGE: {count} messages supprimés au total.")
    return f"{count} messages supprimés."

async def run_punishment(guild_id, user_id, action, reason):
    guild = bot.get_guild(guild_id)
    if not guild: return "Serveur introuvable"
    
    try:
        member = await guild.fetch_member(user_id)
    except:
        return "Membre introuvable (ID invalide ou hors du serveur)."

    try:
        if action == "kick":
            await member.kick(reason=reason)
            add_log(f"KICK: {member.name} expulsé.")
            return f"{member.name} a été kick."
        elif action == "ban":
            await member.ban(reason=reason)
            add_log(f"BAN: {member.name} banni.")
            return f"{member.name} a été ban."
        elif action == "timeout":
            duration = datetime.timedelta(hours=1)
            await member.timeout(duration, reason=reason)
            add_log(f"MUTE: {member.name} réduit au silence (1h).")
            return f"{member.name} est mute pour 1h."
    except Exception as e:
        return f"Erreur permission : {str(e)}"

# --- DASHBOARD WEB ---
app = FastAPI()

@app.get("/leviathan", response_class=HTMLResponse)
async def panel():
    # Génération des options de serveur
    if not bot.guilds:
        guild_opts = "<option>Aucun serveur (Bot Offline)</option>"
    else:
        guild_opts = "".join([f"<option value='{g.id}'>{g.name}</option>" for g in bot.guilds])

    # NOTE : J'ai doublé les accolades {{ }} dans le JS pour éviter le crash Python
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
    </style>
</head>
<body>
    <div class="header">
        <h1>🛡️ LEVIATHAN CONTROL</h1>
        <div>STATUS: <span style="color:#00ff41">ONLINE</span></div>
    </div>

    <div class="card">
        <h3>1. CONNEXION</h3>
        <input type="password" id="key" placeholder="MOT DE PASSE (LEVIATHAN_2026)">
        <select id="guild">{guild_opts}</select>
    </div>

    <div style="display:grid; grid-template-columns: 1fr 1fr; gap:10px;">
        <div class="card">
            <h3>2. SÉCURITÉ</h3>
            <button onclick="send('lockdown', true)">🔒 VERROUILLER (LOCKDOWN)</button>
            <button onclick="send('lockdown', false)">🔓 DÉVERROUILLER</button>
            <button class="danger-btn" onclick="send('purge', 20)">🧹 PURGE (20 msgs)</button>
        </div>
        
        <div class="card">
            <h3>3. SANCTIONS</h3>
            <input type="text" id="target" placeholder="ID UTILISATEUR (Clic Droit -> Copier ID)">
            <button onclick="send('timeout')">🤐 MUTE 1H</button>
            <button class="danger-btn" onclick="send('kick')">👢 KICK</button>
            <button class="danger-btn" onclick="send('ban')">🔨 BAN DÉFINITIF</button>
        </div>
    </div>

    <div class="card">
        <h3>📡 LOGS SYSTÈME</h3>
        <div id="console">En attente de commandes...</div>
    </div>

    <script>
        async function send(action, val=null) {{
            const key = document.getElementById('key').value;
            const guild = document.getElementById('guild').value;
            const target = document.getElementById('target').value;
            
            log(`> Envoi de la commande : ${{action}}...`);

            try {{
                const req = await fetch('/api/run', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{ k: key, g: guild, action: action, val: val, target: target, reason: "Via Panel" }})
                }});
                const res = await req.json();
                
                if(res.error) log(`ERREUR: ${{res.error}}`);
                else log(`RÉPONSE: ${{res.details}}`);
                
            }} catch(e) {{
                log("Erreur de connexion au serveur.");
            }}
        }}

        function log(text) {{
            const con = document.getElementById('console');
            con.innerHTML += `<div>${{text}}</div>`;
            con.scrollTop = con.scrollHeight;
        }}

        // Mise à jour automatique des logs
        setInterval(async () => {{
            try {{
                const r = await fetch('/api/logs');
                const d = await r.json();
                if(d.length > 0) {{
                     // On affiche juste les derniers logs pour pas spammer
                     document.getElementById('console').innerHTML = d.slice().reverse().map(l => `<div>${{l}}</div>`).join('');
                }}
            }} catch(e) {{}}
        }}, 3000);
    </script>
</body>
</html>
"""

@app.post("/api/run")
async def api_run(request: Request):
    try:
        data = await request.json()
        if data.get('k') != ADMIN_KEY: 
            return JSONResponse({"error": "MOT DE PASSE INCORRECT"}, status_code=403)
        
        gid = int(data['g'])
        action = data['action']
        
        if action == 'lockdown':
            res = await run_lockdown(gid, data['val'])
        elif action == 'purge':
            res = await run_purge(gid, int(data['val']))
        elif action in ['kick', 'ban', 'timeout']:
            if not data.get('target'): return {"details": "Il faut un ID utilisateur !"}
            res = await run_punishment(gid, int(data['target']), action, data.get('reason'))
        else:
            res = "Commande inconnue"
            
        return {"status": "ok", "details": res}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/logs")
async def get_logs(): return action_logs

async def main():
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT)
    server = uvicorn.Server(config)
    await asyncio.gather(bot.start(DISCORD_TOKEN), server.serve())

if __name__ == "__main__":
    asyncio.run(main())