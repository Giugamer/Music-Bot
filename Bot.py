# bot.py
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import aiofiles
import json
from mutagen import File as MutagenFile
import ffmpeg
from datetime import timedelta
from dotenv import load_dotenv
from typing import Optional, List

# ─── CONFIG ───────────────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("TOKEN")
AUDIO_DIR = "audio"
PLAYLISTS_FILE = "playlists.json"
QUEUE_FILE = "queue.json"
os.makedirs(AUDIO_DIR, exist_ok=True)

# ─── BOT SETUP ────────────────────────────────────────────────────────────
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ─── GLOBAL STATE ────────────────────────────────────────────────────────
queue: List[str] = []  # upcoming filenames
is_playing: bool = False
voice_client: Optional[discord.VoiceClient] = None
current_audio: Optional[str] = None
current_start: Optional[float] = None
current_duration: float = 0.0

# Persisted nowplaying message per-guild (guild_id -> (channel_id, message_id))
nowplaying_messages: dict[int, tuple[int, int]] = {}

# Playback lock
playback_lock = asyncio.Lock()

# playlists in memory
def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_json(path: str, default):
    if not os.path.exists(path):
        save_json(path, default)
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

playlists = load_json(PLAYLISTS_FILE, {})

# queue persistence structure:
# {
#   "queue": ["a.mp3", ...],
#   "voice": {"guild_id": 123, "channel_id": 456}  # optional
#   "current_audio": "song.mp3"  # optional
# }
def save_queue_file():
    data = {
        "queue": queue,
    }
    # store voice channel to attempt rejoin
    if voice_client and getattr(voice_client, "guild", None) and getattr(voice_client.channel, "id", None):
        data["voice"] = {"guild_id": voice_client.guild.id, "channel_id": voice_client.channel.id}
    if current_audio:
        data["current_audio"] = current_audio
    save_json(QUEUE_FILE, data)

def load_queue_file():
    data = load_json(QUEUE_FILE, {})
    q = data.get("queue", [])
    voice_info = data.get("voice")
    curr = data.get("current_audio")
    return q, voice_info, curr

# ─── UTILITAIRES ──────────────────────────────────────────────────────────
def human_time(seconds: float) -> str:
    try:
        seconds = int(seconds)
    except Exception:
        seconds = 0
    return str(timedelta(seconds=seconds))

def progress_bar(current: float, total: float, size: int = 20) -> str:
    if not total or total <= 0:
        return "▱" * size
    filled = max(0, min(size, int(size * current / total)))
    return "▰" * filled + "▱" * (size - filled)

async def probe_duration(path: str) -> float:
    """Probe duration in thread to avoid blocking."""
    if not os.path.exists(path):
        return 0.0
    def _probe(p):
        try:
            m = MutagenFile(p)
            if m and getattr(m, "info", None):
                return float(m.info.length)
        except Exception:
            pass
        try:
            probe = ffmpeg.probe(p)
            return float(probe['format']['duration'])
        except Exception:
            return 0.0
    return await asyncio.to_thread(_probe, path)

async def total_duration_of_files(files: List[str]) -> float:
    if not files:
        return 0.0
    tasks = [probe_duration(os.path.join(AUDIO_DIR, f)) for f in files]
    results = await asyncio.gather(*tasks)
    return sum(results)

# safe reply helper
async def safe_reply(interaction: discord.Interaction, content: Optional[str] = None, *, embed: Optional[discord.Embed] = None, ephemeral: bool = False):
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)
        else:
            await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
    except discord.NotFound:
        # interaction expired: fallback to channel message
        try:
            if interaction.channel:
                await interaction.channel.send(content)
        except Exception:
            pass
    except Exception:
        # ignore other send errors, fail silently
        pass

# ─── PLAYBACK LOGIC ───────────────────────────────────────────────────────
async def _start_playing_path(path: str):
    """Low-level play using voice_client; must be called with playback_lock acquired."""
    global is_playing, current_start, current_audio, current_duration
    current_duration = await probe_duration(path)
    current_start = asyncio.get_event_loop().time()
    is_playing = True
    # after callback
    def _after(err):
        coro = play_next()
        asyncio.run_coroutine_threadsafe(coro, bot.loop)
    try:
        voice_client.play(discord.FFmpegPCMAudio(path), after=_after)
    except Exception:
        # on error, mark stopped and attempt next
        is_playing = False
        current_audio = None
        current_start = None
        current_duration = 0.0
        await asyncio.sleep(0.5)
        await play_next()

async def play_next():
    """Pop queue and play next file. Called when previous finished."""
    global is_playing, current_audio, current_start, current_duration, voice_client
    async with playback_lock:
        if not queue:
            # nothing to play
            is_playing = False
            current_audio = None
            current_start = None
            current_duration = 0.0
            save_queue_file()
            return
        # get next
        current_audio = queue.pop(0)
        save_queue_file()
        path = os.path.join(AUDIO_DIR, current_audio)
        if not os.path.exists(path):
            # skip missing and continue
            await play_next()
            return
        if not voice_client or not voice_client.is_connected():
            # can't play if not connected - stop and keep queue state
            is_playing = False
            current_audio = None
            current_start = None
            current_duration = 0.0
            return
        await _start_playing_path(path)

# attempt to restore queue and auto-join voice channel on startup
async def restore_and_start():
    global queue, voice_client
    q, voice_info, curr = load_queue_file()
    if q:
        # adopt queue
        queue.clear()
        queue.extend(q)
    # try to rejoin voice channel if info present
    if voice_info:
        guild_id = voice_info.get("guild_id")
        channel_id = voice_info.get("channel_id")
        try:
            guild = bot.get_guild(guild_id) or await bot.fetch_guild(guild_id)
            channel = guild.get_channel(channel_id) or await guild.fetch_channel(channel_id)
            # connect
            try:
                voice_client = await channel.connect()
            except Exception:
                # maybe already connected somewhere, try fetch existing voice_client
                if guild.voice_client:
                    voice_client = guild.voice_client
            # start playing if queue exists
            if queue and (not is_playing):
                await play_next()
        except Exception:
            # ignore errors: we might not have permissions or the channel was removed
            pass
    else:
        # no voice info but queue exists: do not auto connect (requires user to /join)
        if queue and (not is_playing):
            # nothing to do until someone invites the bot to a voice channel
            pass

# ─── NOWPLAYING UPDATER ──────────────────────────────────────────────────
async def nowplaying_updater():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(2)  # update every 2s
        # update each tracked message
        for guild_id, (channel_id, message_id) in list(nowplaying_messages.items()):
            try:
                guild = bot.get_guild(guild_id)
                if not guild:
                    nowplaying_messages.pop(guild_id, None)
                    continue
                # fetch channel and message
                channel = guild.get_channel(channel_id) or await bot.fetch_channel(channel_id)
                try:
                    message = await channel.fetch_message(message_id)
                except discord.NotFound:
                    nowplaying_messages.pop(guild_id, None)
                    continue
                # build embed
                if not current_audio or not is_playing or not voice_client or not voice_client.is_connected():
                    await message.edit(content="❌ Aucune musique en cours.", embed=None)
                    continue
                elapsed = asyncio.get_event_loop().time() - (current_start or 0)
                elapsed = max(0.0, elapsed)
                remaining_current = max(0.0, current_duration - elapsed)
                queue_dur = await total_duration_of_files(queue) if queue else 0.0
                total_remaining = remaining_current + queue_dur
                bar = progress_bar(elapsed, current_duration, size=30)
                embed = discord.Embed(title=f"🎶 Lecture en cours : {current_audio}", color=0x1DB954)
                embed.description = f"{bar}\n`{human_time(elapsed)} / {human_time(current_duration)}`"
                embed.set_footer(text=f"Temps total restant file: {human_time(total_remaining)}")
                await message.edit(content=None, embed=embed)
            except Exception:
                # be resilient: skip problems
                continue

# ─── ON READY ───────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    # sync globally
    try:
        await tree.sync()
    except Exception:
        # ignore sync issues
        pass
    print(f"✅ Bot prêt : {bot.user} — {len(tree.get_commands())} commandes sync")
    # start updater and attempt restore
    bot.loop.create_task(nowplaying_updater())
    bot.loop.create_task(restore_and_start())

# ─── BASIC COMMANDS ─────────────────────────────────────────────────────
@tree.command(name="join", description="Rejoint ton salon vocal")
async def join(interaction: discord.Interaction):
    global voice_client
    if interaction.user.voice and interaction.user.voice.channel:
        channel = interaction.user.voice.channel
        await safe_reply(interaction, "⏳ Connexion au salon...", ephemeral=True)
        try:
            voice_client = await channel.connect()
            save_queue_file()
            await safe_reply(interaction, f"✅ Connecté à {channel.name}", ephemeral=True)
            # if there is a queue and nothing plays, start
            if queue and not is_playing:
                await play_next()
        except Exception as e:
            await safe_reply(interaction, f"❌ Échec connexion : {e}", ephemeral=True)
    else:
        await safe_reply(interaction, "❌ Tu dois être dans un salon vocal.", ephemeral=True)

@tree.command(name="leave", description="Déconnecte le bot du salon vocal")
async def leave(interaction: discord.Interaction):
    global voice_client
    await safe_reply(interaction, "⏳ Déconnexion...", ephemeral=True)
    if voice_client and voice_client.is_connected():
        try:
            await voice_client.disconnect()
        except Exception:
            pass
        voice_client = None
        save_queue_file()
        await safe_reply(interaction, "👋 Déconnecté.", ephemeral=True)
    else:
        await safe_reply(interaction, "❌ Le bot n'est pas connecté.", ephemeral=True)

@tree.command(name="upload", description="Upload un fichier audio dans le bot")
async def upload(interaction: discord.Interaction, fichier: discord.Attachment):
    if not fichier.filename.lower().endswith((".mp3", ".wav", ".ogg", ".m4a")):
        await safe_reply(interaction, "❌ Format non supporté.", ephemeral=True)
        return
    await safe_reply(interaction, "⏳ Téléchargement en cours...", ephemeral=True)
    path = os.path.join(AUDIO_DIR, fichier.filename)
    try:
        async with aiofiles.open(path, "wb") as f:
            await f.write(await fichier.read())
        await safe_reply(interaction, f"✅ Fichier **{fichier.filename}** ajouté.")
    except Exception as e:
        await safe_reply(interaction, f"❌ Erreur lors de l'upload : {e}", ephemeral=True)

@tree.command(name="list", description="Liste tous les fichiers audio disponibles (durée totale incluse)")
async def list_audio(interaction: discord.Interaction):
    await safe_reply(interaction, "⏳ Calcul des durées...", ephemeral=True)
    files = [f for f in os.listdir(AUDIO_DIR) if f.lower().endswith((".mp3", ".wav", ".ogg", ".m4a"))]
    if not files:
        await safe_reply(interaction, "🎵 Aucun fichier trouvé.")
        return
    total = await total_duration_of_files(files)
    msg = f"**🎶 {len(files)} fichiers — Durée totale : `{human_time(total)}`**\n\n"
    # compute durations per file but concurrently
    for f in files:
        dur = await probe_duration(os.path.join(AUDIO_DIR, f))
        msg += f"• `{f}` — `{human_time(dur)}`\n"
    await safe_reply(interaction, msg)

@tree.command(name="play", description="Joue un fichier audio local")
async def play(interaction: discord.Interaction, nom: str):
    global voice_client
    path = os.path.join(AUDIO_DIR, nom)
    if not os.path.exists(path):
        await safe_reply(interaction, "❌ Fichier introuvable.", ephemeral=True)
        return
    if not interaction.user.voice or not interaction.user.voice.channel:
        await safe_reply(interaction, "❌ Tu dois être dans un salon vocal.", ephemeral=True)
        return
    # immediate ack
    await safe_reply(interaction, "⏳ Mise en file...", ephemeral=True)
    # ensure connected
    if not voice_client or not voice_client.is_connected():
        try:
            voice_client = await interaction.user.voice.channel.connect()
        except Exception as e:
            # send followup error
            await safe_reply(interaction, f"❌ Impossible de rejoindre le salon : {e}", ephemeral=True)
            return
    queue.append(nom)
    save_queue_file()
    await interaction.followup.send(f"🎧 Ajouté à la file d’attente : `{nom}`")
    if not is_playing:
        await play_next()

@tree.command(name="playall", description="Joue tout le dossier audio ou une playlist (optionnel: playlist name)")
@app_commands.describe(playlist="Nom de la playlist à jouer (optionnel)")
async def playall(interaction: discord.Interaction, playlist: Optional[str] = None):
    global voice_client
    await safe_reply(interaction, "⏳ Préparation...", ephemeral=True)
    files_to_add: List[str] = []
    if playlist:
        if playlist not in playlists:
            await safe_reply(interaction, f"❌ Playlist `{playlist}` introuvable.", ephemeral=True)
            return
        files_to_add = playlists[playlist].copy()
    else:
        files_to_add = [f for f in os.listdir(AUDIO_DIR) if f.lower().endswith((".mp3", ".wav", ".ogg", ".m4a"))]
        files_to_add.sort()
    if not files_to_add:
        await safe_reply(interaction, "❌ Aucun fichier à jouer.", ephemeral=True)
        return
    # connect if needed
    if not voice_client or not voice_client.is_connected():
        if not interaction.user.voice or not interaction.user.voice.channel:
            await safe_reply(interaction, "❌ Tu dois être dans un salon vocal pour utiliser /playall.", ephemeral=True)
            return
        try:
            voice_client = await interaction.user.voice.channel.connect()
        except Exception as e:
            await safe_reply(interaction, f"❌ Impossible de rejoindre : {e}", ephemeral=True)
            return
    added = 0
    for f in files_to_add:
        if os.path.exists(os.path.join(AUDIO_DIR, f)):
            queue.append(f)
            added += 1
    save_queue_file()
    await interaction.followup.send(f"▶️ {added} fichiers ajoutés à la file d'attente.")
    if not is_playing:
        await play_next()

@tree.command(name="pause", description="Met la musique en pause")
async def pause(interaction: discord.Interaction):
    await safe_reply(interaction, "⏳ Pause...", ephemeral=True)
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        await safe_reply(interaction, "⏸️ Musique mise en pause.")
    else:
        await safe_reply(interaction, "❌ Aucune musique en cours.", ephemeral=True)

@tree.command(name="resume", description="Reprend la musique")
async def resume(interaction: discord.Interaction):
    await safe_reply(interaction, "⏳ Reprise...", ephemeral=True)
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        await safe_reply(interaction, "▶️ Musique reprise.")
    else:
        await safe_reply(interaction, "❌ Aucune musique en pause.", ephemeral=True)

@tree.command(name="skip", description="Passe à la musique suivante")
async def skip(interaction: discord.Interaction):
    await safe_reply(interaction, "⏳ Passage...", ephemeral=True)
    if voice_client and voice_client.is_playing():
        voice_client.stop()  # will trigger after callback play_next
        await safe_reply(interaction, "⏭️ Musique passée.")
    else:
        if queue:
            await play_next()
            await safe_reply(interaction, "⏭️ Musique passée (lecture lancée).")
        else:
            await safe_reply(interaction, "❌ Rien à passer.", ephemeral=True)

@tree.command(name="stop", description="Arrête la lecture et vide la file")
async def stop(interaction: discord.Interaction):
    global queue, is_playing
    await safe_reply(interaction, "⏳ Arrêt en cours...", ephemeral=True)
    queue.clear()
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()
    is_playing = False
    save_queue_file()
    await safe_reply(interaction, "⛔ Lecture arrêtée et file vidée.")

@tree.command(name="queue", description="Affiche la file d’attente et la durée totale restante")
async def show_queue(interaction: discord.Interaction):
    await safe_reply(interaction, "⏳ Récupération de la file...", ephemeral=True)
    if not queue and not current_audio:
        await safe_reply(interaction, "🕳️ La file est vide.")
        return
    lines = []
    idx = 1
    for s in queue:
        lines.append(f"{idx}. {s}")
        idx += 1
    remaining_current = 0.0
    if current_audio and is_playing:
        elapsed = asyncio.get_event_loop().time() - (current_start or 0)
        remaining_current = max(0.0, current_duration - elapsed)
    queue_dur = await total_duration_of_files(queue) if queue else 0.0
    total_remaining = remaining_current + queue_dur
    msg = "**🎵 File d’attente :**\n" + ("\n".join(lines) if lines else "(rien dans la file)") + f"\n\n⏱️ Temps total restant : `{human_time(total_remaining)}`"
    await safe_reply(interaction, msg)

@tree.command(name="nowplaying", description="Affiche la musique en cours (message mis à jour toutes les 2s)")
async def nowplaying(interaction: discord.Interaction):
    await safe_reply(interaction, "⏳ Chargement nowplaying...", ephemeral=True)
    if not current_audio or not is_playing:
        await interaction.followup.send("❌ Aucune musique en cours.")
        return
    elapsed = asyncio.get_event_loop().time() - (current_start or 0)
    remaining_current = max(0.0, current_duration - elapsed)
    queue_dur = await total_duration_of_files(queue) if queue else 0.0
    total_remaining = remaining_current + queue_dur
    bar = progress_bar(elapsed, current_duration, size=30)
    embed = discord.Embed(title=f"🎶 Lecture en cours : {current_audio}", color=0x1DB954)
    embed.description = f"{bar}\n`{human_time(elapsed)} / {human_time(current_duration)}`"
    embed.set_footer(text=f"Temps total restant file: {human_time(total_remaining)}")
    sent = await interaction.followup.send(embed=embed)
    if interaction.guild:
        nowplaying_messages[interaction.guild.id] = (sent.channel.id, sent.id)

# ─── PLAYLIST GROUP WITH AUTOCOMPLETE ───────────────────────────────────
playlist_group = app_commands.Group(name="playlist", description="Gestion des playlists globales")

# autocomplete function for playlist names
async def playlist_name_autocomplete(interaction: discord.Interaction, current: str):
    choices = []
    for name in playlists.keys():
        if current.lower() in name.lower():
            choices.append(app_commands.Choice(name=name, value=name))
    # limit choices to 25 (discord limit)
    return choices[:25]

@playlist_group.command(name="create", description="Crée une playlist globale")
async def pl_create(interaction: discord.Interaction, name: str):
    await safe_reply(interaction, "⏳ Création...", ephemeral=True)
    if name in playlists:
        await safe_reply(interaction, f"❌ La playlist `{name}` existe déjà.", ephemeral=True)
        return
    playlists[name] = []
    save_json(PLAYLISTS_FILE, playlists)
    await safe_reply(interaction, f"✅ Playlist `{name}` créée.")

@playlist_group.command(name="add", description="Ajoute un fichier à une playlist")
@app_commands.describe(name="Nom de la playlist", fichier="Nom du fichier (dans le dossier audio)")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def pl_add(interaction: discord.Interaction, name: str, fichier: str):
    await safe_reply(interaction, "⏳ Ajout...", ephemeral=True)
    if name not in playlists:
        await safe_reply(interaction, f"❌ Playlist `{name}` introuvable.", ephemeral=True)
        return
    if not os.path.exists(os.path.join(AUDIO_DIR, fichier)):
        await safe_reply(interaction, f"❌ Le fichier `{fichier}` n'existe pas.", ephemeral=True)
        return
    playlists[name].append(fichier)
    save_json(PLAYLISTS_FILE, playlists)
    await safe_reply(interaction, f"✅ `{fichier}` ajouté à `{name}`.")

@playlist_group.command(name="remove", description="Supprime un fichier d'une playlist")
@app_commands.describe(name="Nom de la playlist", fichier="Nom du fichier")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def pl_remove(interaction: discord.Interaction, name: str, fichier: str):
    await safe_reply(interaction, "⏳ Suppression...", ephemeral=True)
    if name not in playlists:
        await safe_reply(interaction, f"❌ Playlist `{name}` introuvable.", ephemeral=True)
        return
    if fichier not in playlists[name]:
        await safe_reply(interaction, f"❌ `{fichier}` n'est pas dans `{name}`.", ephemeral=True)
        return
    playlists[name].remove(fichier)
    save_json(PLAYLISTS_FILE, playlists)
    await safe_reply(interaction, f"✅ `{fichier}` supprimé de `{name}`.")

@playlist_group.command(name="list", description="Liste les playlists et leur contenu")
async def pl_list(interaction: discord.Interaction):
    await safe_reply(interaction, "⏳ Récupération playlists...", ephemeral=True)
    if not playlists:
        await safe_reply(interaction, "📂 Aucune playlist enregistrée.")
        return
    lines = []
    for name, items in playlists.items():
        total = await total_duration_of_files(items) if items else 0.0
        lines.append(f"• `{name}` — {len(items)} fichiers — Durée totale : `{human_time(total)}`")
        for it in items:
            lines.append(f"    • `{it}`")
    await safe_reply(interaction, "\n".join(lines))

@playlist_group.command(name="delete", description="Supprime une playlist")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def pl_delete(interaction: discord.Interaction, name: str):
    await safe_reply(interaction, "⏳ Suppression...", ephemeral=True)
    if name not in playlists:
        await safe_reply(interaction, f"❌ Playlist `{name}` introuvable.", ephemeral=True)
        return
    playlists.pop(name)
    save_json(PLAYLISTS_FILE, playlists)
    await safe_reply(interaction, f"✅ Playlist `{name}` supprimée.")

tree.add_command(playlist_group)

# ─── SHUTDOWN HANDLING ──────────────────────────────────────────────────
# Save queue/playlists on shutdown
@bot.event
async def on_disconnect():
    try:
        save_json(PLAYLISTS_FILE, playlists)
        save_queue_file()
    except Exception:
        pass

@bot.event
async def on_close():
    try:
        save_json(PLAYLISTS_FILE, playlists)
        save_queue_file()
    except Exception:
        pass

# ensure saving on normal exit
import atexit
def _atexit_save():
    try:
        save_json(PLAYLISTS_FILE, playlists)
        save_queue_file()
    except Exception:
        pass
atexit.register(_atexit_save)

# ─── RUN ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        print("Error: TOKEN not set in .env")
        raise SystemExit(1)
    bot.run(TOKEN)
