import os
import json
import asyncio
import datetime
import discord
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from discord.ext import commands

# --- CONFIGURATION ---
PORT = int(os.getenv("PORT", "10000"))
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "METS_TON_TOKEN_ICI").strip()
ADMIN_KEY = os.getenv("ADMIN_KEY", "LEVIATHAN_2026").strip()

# --- BOT CORE ---
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
action_logs = []

def add_log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    action_logs.append(f"[{ts}] {msg}")
    if len(action_logs) > 50: action_logs.pop(0) # On garde les 50 derniers logs

# --- MOTEUR DE SÉCURITÉ (SAFE MODE) ---

async def run_lockdown(guild):
    add_log(f"🔒 LOCKDOWN : Verrouillage de {guild.name}...")
    role = guild.default_role
    count = 0
    for channel in guild.text_channels:
        try:
            # On écrase les permissions pour interdire d'écrire
            overwrite = channel.overwrites_for(role)
            overwrite.send_messages = False
            await channel.set_permissions(role, overwrite=overwrite)
            count += 1
        except: continue
    add_log(f"🔒 LOCKDOWN : {count} salons verrouillés.")

async def run_unlockdown(guild):
    add_log(f"🔓 UNLOCK : Déverrouillage de {guild.name}...")
    role = guild.default_role
    count = 0
    for channel in guild.text_channels:
        try:
            overwrite = channel.overwrites_for(role)
            overwrite.send_messages = True # On réautorise
            await channel.set_permissions(role, overwrite=overwrite)
            count += 1
        except: continue
    add_log(f"🔓 UNLOCK : {count} salons ouverts.")

# --- SURVEILLANCE TEMPS RÉEL ---

@bot.event
async def on_message(message):
    if message.author.bot: return
    # Ajoute le message dans les logs du Panel
    add_log(f"MSG | {message.author.name}: {message.content[:30]}") # Aperçu des 30 premiers caractères
    await bot.process_commands(message)

@bot.event
async def on_member_join(member):
    add_log(f"JOIN | {member.name} a rejoint le serveur.")

@bot.event
async def on_member_remove(member):
    add_log(f"LEAVE | {member.name} a quitté le serveur.")

# --- DASHBOARD MODIFIÉ ---
app = FastAPI()

@app.get("/leviathan", response_class=HTMLResponse)
async def panel():
    guild_options = "".join([f"<option value='{g.id}'>{g.name}</option>" for g in bot.guilds])
    return f"""
<!DOCTYPE html>
<html>
<head><title>LEVIATHAN SECURITY</title>
<style>
    body {{ background:#050505; color:#0f0; font-family:'Courier New', monospace; padding:20px; }}
    .card {{ border:1px solid #0f0; padding:15px; background:#000; margin-bottom:15px; box-shadow:0 0 5px #0f0; }}
    .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:15px; }}
    button {{ width:100%; padding:12px; margin:5px 0; background:#111; color:#0f0; border:1px solid #0f0; cursor:pointer; font-weight:bold; transition:0.2s; }}
    button:hover {{ background:#0f0; color:#000; }}
    input, select {{ width:100%; padding:12px; margin:5px 0; background:#111; color:#0f0; border:1px solid #0f0; }}
    .log-box {{ background:#000; height:300px; overflow-y:scroll; border:1px solid #333; padding:10px; font-size:11px; color:#0f0; font-family:monospace; }}
    h3 {{ margin-top:0; border-bottom:1px solid #333; padding-bottom:5px; }}
    .status {{ color: cyan; font-weight: bold; }}
</style></head>
<body>
    <h1>🛡️ LEVIATHAN : SECURITY CENTER</h1>
    <div class="card">
        <input type="password" id="k" placeholder="ADMIN_KEY (LEVIATHAN_2026)">
        <select id="g">{guild_options}</select>
    </div>
    
    <div class="grid">
        <div class="card">
            <h3>🔒 CONTRÔLE D'URGENCE</h3>
            <button onclick="exe('lockdown')" style="border-color:orange; color:orange;">🔒 LOCKDOWN GLOBAL (Verrouiller)</button>
            <button onclick="exe('unlock')" style="border-color:cyan; color:cyan;">🔓 UNLOCK GLOBAL (Ouvrir)</button>
        </div>

        <div class="card">
            <h3>📂 SAUVEGARDES</h3>
            <button onclick="exe('backup')">💾 BACKUP CONFIGURATION (JSON)</button>
            <button onclick="exe('restore')">♻️ RESTAURER CONFIGURATION</button>
        </div>
    </div>

    <div class="card">
        <h3>👁️ LOGS & SURVEILLANCE EN DIRECT</h3>
        <div class="log-box" id="logs">
            Initialisation du système de surveillance...<br>
            En attente de données...
        </div>
    </div>

    <script>
        // Fonction d'exécution
        async function exe(a){{
            const d = {{
                k: document.getElementById('k').value, 
                g: document.getElementById('g').value, 
                action: a
            }};
            const r = await fetch('/api/run', {{
                method:'POST', 
                headers:{{'Content-Type':'application/json'}}, 
                body:JSON.stringify(d)
            }});
            const res = await r.json(); 
            if(res.error) alert("ERREUR: " + res.error);
        }}

        // Boucle de mise à jour des logs (Auto-Refresh)
        setInterval(async () => {{
            const r = await fetch('/api/logs'); 
            const d = await r.json();
            // Inverse l'ordre pour avoir les nouveaux en haut
            document.getElementById('logs').innerHTML = d.slice().reverse().join('<br>');
        }}, 1500); // Mise à jour toutes les 1.5 secondes
    </script>
</body></html>"""

@app.post("/api/run")
async def api_run(request: Request):
    d = await request.json()
    if d['k'] != ADMIN_KEY: return JSONResponse({"error":"Clé invalide"}, status_code=403)
    
    guild = bot.get_guild(int(d['g']))
    if not guild: return JSONResponse({"error":"Serveur introuvable"}, status_code=404)
    owner = (await bot.application_info()).owner

    # --- ACTIONS ---
    if d['action'] == 'lockdown':
        asyncio.create_task(run_lockdown(guild))
    
    elif d['action'] == 'unlock':
        asyncio.create_task(run_unlockdown(guild))

    elif d['action'] == 'backup':
        # Sauvegarde propre (Structure uniquement)
        data = {
            "roles": [{"n": r.name, "c": r.color.value} for r in guild.roles if not r.managed],
            "categories": [{"name": c.name, "channels": [ch.name for ch in c.channels]} for c in guild.categories]
        }
        filename = f"backup_{guild.id}.json"
        with open(filename, "w") as f: json.dump(data, f)
        await owner.send(f"📦 Backup de sécurité pour **{guild.name}**", file=discord.File(filename))
        add_log("Backup de sécurité envoyée en MP.")

    # (Note: La restauration complète est complexe, ici on log juste la demande pour l'instant)
    elif d['action'] == 'restore':
        add_log("⚠️ Fonction Restore activée : En attente de fichier manuel.")

    return {"status": "Action lancée"}

@app.get("/api/logs")
async def get_logs(): return action_logs

@bot.event
async def on_ready():
    add_log(f"SYSTÈME EN LIGNE : Connecté en tant que {bot.user}")

async def main():
    # Lance le serveur Web et le Bot en même temps
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT)
    server = uvicorn.Server(config)
    await asyncio.gather(server.serve(), bot.start(DISCORD_TOKEN))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Arrêt du système.")