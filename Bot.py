import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio, os, aiofiles, json
from mutagen import File as MutagenFile
import ffmpeg
from datetime import timedelta

# ─── CONFIG ────────────────────────────────────────────────────────────────
from dotenv import load_dotenv
import os

load_dotenv()
TOKEN = os.getenv("TOKEN")
AUDIO_DIR = "audio"
PLAYLISTS_DIR = "playlists"
DATA_DIR = "data"

# ─── CRÉER LES DOSSIERS SI MANQUANT ────────────────────────────────────────
for folder in [AUDIO_DIR, PLAYLISTS_DIR, DATA_DIR]:
    os.makedirs(folder, exist_ok=True)
    print(f"📂 Dossier prêt : {folder}")

# ─── SETUP DU BOT ──────────────────────────────────────────────────────────
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ─── VARIABLES MULTI-SERVEUR ──────────────────────────────────────────────
queues = {}        # queues[guild_id] = [nom_audio,...]
current_audio = {} # current_audio[guild_id] = fichier en cours
current_start = {} # current_start[guild_id] = timestamp
voice_clients = {} # voice_clients[guild_id] = VoiceClient
is_playing = {}    # is_playing[guild_id] = bool

# ─── UTILITAIRES ──────────────────────────────────────────────────────────
def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"💾 JSON sauvegardé : {path}")

def load_json(path: str):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def get_queue_path(guild_id):
    return os.path.join(DATA_DIR, f"queue_{guild_id}.json")

def get_playlist_path(guild_id):
    return os.path.join(PLAYLISTS_DIR, f"{guild_id}.json")

def human_time(seconds):
    return str(timedelta(seconds=int(seconds)))

def get_audio_duration(path):
    try:
        m = MutagenFile(path)
        if m and m.info:
            return m.info.length
    except:
        pass
    try:
        probe = ffmpeg.probe(path)
        return float(probe['format']['duration'])
    except:
        return 0

def progress_bar(current, total, size=20):
    filled = int(size * current / total) if total else 0
    empty = size - filled
    return "▰" * filled + "▱" * empty

# ─── QUEUE / LECTURE ──────────────────────────────────────────────────────
async def play_next(guild_id):
    if guild_id not in queues or not queues[guild_id]:
        is_playing[guild_id] = False
        current_audio[guild_id] = None
        return

    is_playing[guild_id] = True
    current_audio[guild_id] = queues[guild_id].pop(0)
    current_start[guild_id] = asyncio.get_event_loop().time()
    path = os.path.join(AUDIO_DIR, current_audio[guild_id])
    vc = voice_clients[guild_id]

    def after_play(error):
        fut = asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print("Erreur play_next:", e)

    vc.play(discord.FFmpegPCMAudio(path), after=after_play)
    save_json(get_queue_path(guild_id), queues[guild_id])

# ─── EVENTS ───────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await tree.sync()  # global pour tous les serveurs
    print(f"✅ Bot prêt : {bot.user} — {len(tree.get_commands())} commandes sync")
    bot.loop.create_task(nowplaying_updater())

# ─── COMMANDE /join ───────────────────────────────────────────────────────
@tree.command(name="join", description="Rejoint ton salon vocal")
async def join(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    if interaction.user.voice:
        channel = interaction.user.voice.channel
        vc = await channel.connect()
        voice_clients[guild_id] = vc
        await interaction.response.send_message(f"✅ Connecté à {channel.name}", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Tu dois être dans un salon vocal", ephemeral=True)

# ─── COMMANDE /leave ──────────────────────────────────────────────────────
@tree.command(name="leave", description="Déconnecte le bot")
async def leave(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    vc = voice_clients.get(guild_id)
    if vc and vc.is_connected():
        await vc.disconnect()
        voice_clients[guild_id] = None
        await interaction.response.send_message("👋 Déconnecté.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Le bot n'est pas connecté.", ephemeral=True)

# ─── COMMANDE /upload ─────────────────────────────────────────────────────
@tree.command(name="upload", description="Upload un fichier audio")
async def upload(interaction: discord.Interaction, fichier: discord.Attachment):
    if not fichier.filename.lower().endswith((".mp3", ".wav", ".ogg", ".m4a")):
        await interaction.response.send_message("❌ Format non supporté.", ephemeral=True)
        return
    path = os.path.join(AUDIO_DIR, fichier.filename)
    async with aiofiles.open(path, "wb") as f:
        await f.write(await fichier.read())
    await interaction.response.send_message(f"✅ Fichier **{fichier.filename}** ajouté.")

# ─── COMMANDE /list ───────────────────────────────────────────────────────
@tree.command(name="list", description="Liste les fichiers audio avec durée")
async def list_audio(interaction: discord.Interaction):
    files = [f for f in os.listdir(AUDIO_DIR) if f.lower().endswith((".mp3", ".wav", ".ogg", ".m4a"))]
    if not files:
        await interaction.response.send_message("🎵 Aucun fichier trouvé.")
        return
    msg = "**🎶 Fichiers disponibles :**\n"
    total = 0
    for f in files:
        dur = get_audio_duration(os.path.join(AUDIO_DIR, f))
        total += dur
        msg += f"• `{f}` — {human_time(dur)}\n"
    msg += f"\n⏱️ Temps total de toutes les musiques : {human_time(total)}"
    await interaction.response.send_message(msg)

# ─── COMMANDE /play ───────────────────────────────────────────────────────
@tree.command(name="play", description="Joue un fichier audio local")
async def play(interaction: discord.Interaction, nom: str):
    guild_id = interaction.guild.id
    path = os.path.join(AUDIO_DIR, nom)
    if not os.path.exists(path):
        await interaction.response.send_message("❌ Fichier introuvable.")
        return
    if not interaction.user.voice:
        await interaction.response.send_message("❌ Tu dois être dans un salon vocal.")
        return
    if guild_id not in voice_clients or not voice_clients[guild_id] or not voice_clients[guild_id].is_connected():
        vc = await interaction.user.voice.channel.connect()
        voice_clients[guild_id] = vc

    queues.setdefault(guild_id, []).append(nom)
    save_json(get_queue_path(guild_id), queues[guild_id])
    await interaction.response.send_message(f"🎧 Ajouté à la file : `{nom}`")
    if not is_playing.get(guild_id, False):
        await play_next(guild_id)

# ─── COMMANDE /playall ────────────────────────────────────────────────────
@tree.command(name="playall", description="Joue tous les fichiers audio")
async def playall(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    files = [f for f in os.listdir(AUDIO_DIR) if f.lower().endswith((".mp3", ".wav", ".ogg", ".m4a"))]
    if not files:
        await interaction.response.send_message("🎵 Aucun fichier audio trouvé.")
        return
    queues[guild_id] = files
    save_json(get_queue_path(guild_id), queues[guild_id])
    if not voice_clients.get(guild_id) or not voice_clients[guild_id].is_connected():
        if interaction.user.voice:
            vc = await interaction.user.voice.channel.connect()
            voice_clients[guild_id] = vc
    await interaction.response.send_message(f"🎶 Tous les fichiers ont été ajoutés à la file ({len(files)}).")
    if not is_playing.get(guild_id, False):
        await play_next(guild_id)

# ─── AUTRES COMMANDES ─────────────────────────────────────────────────────
@tree.command(name="pause", description="Met en pause")
async def pause(interaction: discord.Interaction):
    vc = voice_clients.get(interaction.guild.id)
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Musique mise en pause.")
    else:
        await interaction.response.send_message("❌ Aucune musique en cours.")

@tree.command(name="resume", description="Reprend la lecture")
async def resume(interaction: discord.Interaction):
    vc = voice_clients.get(interaction.guild.id)
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Musique reprise.")
    else:
        await interaction.response.send_message("❌ Aucune musique en pause.")

@tree.command(name="skip", description="Passe à la musique suivante")
async def skip(interaction: discord.Interaction):
    vc = voice_clients.get(interaction.guild.id)
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("⏭️ Musique passée.")
    else:
        await interaction.response.send_message("❌ Rien à passer.")

@tree.command(name="stop", description="Arrête et vide la queue")
async def stop(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    queues[guild_id] = []
    save_json(get_queue_path(guild_id), queues[guild_id])
    vc = voice_clients.get(guild_id)
    if vc and vc.is_playing():
        vc.stop()
    is_playing[guild_id] = False
    current_audio[guild_id] = None
    await interaction.response.send_message("⛔ Lecture arrêtée et queue vidée.")

@tree.command(name="queue", description="Affiche la file d’attente")
async def show_queue(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    q = queues.get(guild_id, [])
    if not q:
        await interaction.response.send_message("🕳️ La file est vide.")
        return
    msg = "**🎵 File d’attente :**\n"
    for i, song in enumerate(q, 1):
        msg += f"{i}. {song}\n"
    await interaction.response.send_message(msg)

# ─── COMMANDE /nowplaying ─────────────────────────────────────────────────
@tree.command(name="nowplaying", description="Musique en cours")
async def nowplaying(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    if not current_audio.get(guild_id):
        await interaction.response.send_message("❌ Aucune musique en cours.")
        return

    dur = get_audio_duration(os.path.join(AUDIO_DIR, current_audio[guild_id]))
    elapsed = asyncio.get_event_loop().time() - current_start[guild_id]
    remaining = elapsed
    total_remaining = dur
    for song in queues.get(guild_id, []):
        total_remaining += get_audio_duration(os.path.join(AUDIO_DIR, song))

    bar = progress_bar(elapsed, dur)
    embed = discord.Embed(
        title=f"🎶 Lecture en cours : {current_audio[guild_id]}",
        description=f"{bar}\n`{human_time(elapsed)} / {human_time(dur)}`\n⏱️ Temps total restant : {human_time(total_remaining)}",
        color=0x1DB954,
    )
    await interaction.response.send_message(embed=embed)

# ─── TÂCHE / NOWPLAYING UPDATER ───────────────────────────────────────────
async def nowplaying_updater():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(2)
        for guild_id, vc in voice_clients.items():
            if current_audio.get(guild_id) and vc and vc.is_playing():
                try:
                    await bot.change_presence(activity=discord.Activity(
                        type=discord.ActivityType.listening,
                        name=current_audio[guild_id]
                    ))
                except:
                    pass

# ─── DÉMARRAGE DU BOT ─────────────────────────────────────────────────────
bot.run(TOKEN)
