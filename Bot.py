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
from typing import Optional, List, Dict

# ─── CONFIG ───────────────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("TOKEN")

AUDIO_DIR = "audio"
PLAYLISTS_DIR = "playlists"
DATA_DIR = "data"  # per-guild queue files
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(PLAYLISTS_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# ─── BOT SETUP ────────────────────────────────────────────────────────────
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ─── PER-GUILD STATE ─────────────────────────────────────────────────────
# Each guild has its own queue and playback state
queues: Dict[int, List[str]] = {}               # guild_id -> [filenames]
is_playing: Dict[int, bool] = {}                # guild_id -> bool
current_audio: Dict[int, Optional[str]] = {}    # guild_id -> current filename
current_start: Dict[int, Optional[float]] = {}  # guild_id -> loop time
current_duration: Dict[int, float] = {}         # guild_id -> float

# nowplaying message tracking per guild (guild_id -> (channel_id, message_id))
nowplaying_messages: Dict[int, tuple[int, int]] = {}

# playback lock per guild
playback_locks: Dict[int, asyncio.Lock] = {}

# utility for file lists
AUDIO_EXTS = (".mp3", ".wav", ".ogg", ".m4a")

# ─── HELPERS FOR FILE I/O ──────────────────────────────────────────────────
def json_path_playlists(guild_id: int) -> str:
    return os.path.join(PLAYLISTS_DIR, f"{guild_id}.json")

def json_path_queue(guild_id: int) -> str:
    return os.path.join(DATA_DIR, f"queue_{guild_id}.json")

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

def list_audio_files() -> List[str]:
    return sorted([f for f in os.listdir(AUDIO_DIR) if f.lower().endswith(AUDIO_EXTS)])

# ─── DURATION PROBING (offload to thread) ─────────────────────────────────
async def probe_duration(path: str) -> float:
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

# ─── TIME & PROGRESS BAR ──────────────────────────────────────────────────
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

# ─── SAFE REPLY (avoids Unknown interaction) ──────────────────────────────
async def safe_reply(interaction: discord.Interaction, content: Optional[str] = None, *, embed: Optional[discord.Embed] = None, ephemeral: bool = False):
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)
        else:
            await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
    except discord.NotFound:
        # interaction expired; fallback to channel send
        try:
            if interaction.channel:
                await interaction.channel.send(content or (embed.to_dict() if embed else None))
        except Exception:
            pass
    except Exception:
        pass

# ─── PLAYBACK CORE (per-guild) ───────────────────────────────────────────
def ensure_guild_state(guild_id: int):
    queues.setdefault(guild_id, [])
    is_playing.setdefault(guild_id, False)
    current_audio.setdefault(guild_id, None)
    current_start.setdefault(guild_id, None)
    current_duration.setdefault(guild_id, 0.0)
    playback_locks.setdefault(guild_id, asyncio.Lock())

async def _start_playing_path(guild_id: int, voice_client: discord.VoiceClient, path: str):
    """Start low-level playback for guild_id. Must be called with lock held."""
    global is_playing, current_audio, current_start, current_duration
    dur = await probe_duration(path)
    current_duration[guild_id] = dur
    current_start[guild_id] = asyncio.get_event_loop().time()
    is_playing[guild_id] = True

    def _after(err):
        # schedule play_next for this guild
        coro = play_next(guild_id)
        asyncio.run_coroutine_threadsafe(coro, bot.loop)

    try:
        voice_client.play(discord.FFmpegPCMAudio(path), after=_after)
    except Exception:
        # on error reset and try next
        is_playing[guild_id] = False
        current_audio[guild_id] = None
        current_start[guild_id] = None
        current_duration[guild_id] = 0.0
        await asyncio.sleep(0.5)
        await play_next(guild_id)

async def play_next(guild_id: int):
    """Pop queue and play next track for given guild."""
    ensure_guild_state(guild_id)
    async with playback_locks[guild_id]:
        if not queues[guild_id]:
            is_playing[guild_id] = False
            current_audio[guild_id] = None
            current_start[guild_id] = None
            current_duration[guild_id] = 0.0
            # persist queue
            save_queue_for_guild(guild_id)
            return

        # pop next
        current_audio[guild_id] = queues[guild_id].pop(0)
        save_queue_for_guild(guild_id)
        filename = current_audio[guild_id]
        path = os.path.join(AUDIO_DIR, filename)
        if not os.path.exists(path):
            # skip missing
            await play_next(guild_id)
            return

        # get voice client from guild
        guild = bot.get_guild(guild_id)
        voice_client = None
        if guild:
            voice_client = guild.voice_client
        if not voice_client or not voice_client.is_connected():
            # can't play until connected; put back current track at front and stop playing
            queues[guild_id].insert(0, filename)
            current_audio[guild_id] = None
            is_playing[guild_id] = False
            current_start[guild_id] = None
            current_duration[guild_id] = 0.0
            save_queue_for_guild(guild_id)
            return

        await _start_playing_path(guild_id, voice_client, path)

# ─── PERSISTENCE PER GUILD ───────────────────────────────────────────────
def save_playlists_for_guild(guild_id: int, data):
    save_json(json_path_playlists(guild_id), data)

def load_playlists_for_guild(guild_id: int):
    return load_json(json_path_playlists(guild_id), {})

def save_queue_for_guild(guild_id: int):
    data = {"queue": queues.get(guild_id, [])}
    # try to save voice channel to attempt rejoin (if connected)
    guild = bot.get_guild(guild_id)
    if guild:
        vc = guild.voice_client
        if vc and getattr(vc, "channel", None):
            data["voice"] = {"guild_id": guild_id, "channel_id": vc.channel.id}
    if current_audio.get(guild_id):
        data["current_audio"] = current_audio[guild_id]
    save_json(json_path_queue(guild_id), data)

def load_queue_for_guild(guild_id: int):
    data = load_json(json_path_queue(guild_id), {})
    q = data.get("queue", [])
    voice_info = data.get("voice")
    curr = data.get("current_audio")
    return q, voice_info, curr

# Save all playlists and queues on exit
def save_all_state():
    # playlists saved individually when modified
    for gid in list(queues.keys()):
        try:
            save_queue_for_guild(gid)
        except Exception:
            pass

import atexit
atexit.register(save_all_state)

# ─── RESTORE QUEUE & AUTO-REJOIN ────────────────────────────────────────
async def restore_all_queues_and_maybe_join():
    # scan data dir for queue files and restore
    for fname in os.listdir(DATA_DIR):
        if not fname.startswith("queue_") or not fname.endswith(".json"):
            continue
        try:
            gid = int(fname[len("queue_"):-len(".json")])
        except Exception:
            continue
        q, voice_info, curr = load_queue_for_guild(gid)
        ensure_guild_state(gid)
        queues[gid] = q
        if voice_info:
            guild_id = voice_info.get("guild_id")
            channel_id = voice_info.get("channel_id")
            try:
                guild = bot.get_guild(guild_id)
                if not guild:
                    continue
                # try to fetch channel
                channel = guild.get_channel(channel_id) or await guild.fetch_channel(channel_id)
                if channel:
                    # try connect
                    try:
                        # connect only if bot not already in that guild
                        if not guild.voice_client:
                            await channel.connect()
                    except Exception:
                        # ignore join failure
                        pass
                # if we are connected now and queue non-empty, start playing
                if guild.voice_client and queues.get(gid):
                    # start playback if not already playing
                    if not is_playing.get(gid, False):
                        bot.loop.create_task(play_next(gid))
            except Exception:
                continue
        else:
            # no voice info: we restored queue but won't auto-join
            if q:
                # leave as is (will start when someone calls /join)
                pass

# ─── NOWPLAYING UPDATER (edits messages every 2s) ────────────────────────
async def nowplaying_updater():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(2)
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

                ensure_guild_state(guild_id)
                if not current_audio.get(guild_id) or not is_playing.get(guild_id, False):
                    await message.edit(content="❌ Aucune musique en cours.", embed=None)
                    continue

                elapsed = asyncio.get_event_loop().time() - (current_start.get(guild_id, 0) or 0)
                elapsed = max(0.0, elapsed)
                remaining_current = max(0.0, current_duration.get(guild_id, 0.0) - elapsed)
                queue_dur = await total_duration_of_files(queues.get(guild_id, [])) if queues.get(guild_id) else 0.0
                total_remaining = remaining_current + queue_dur
                bar = progress_bar(elapsed, current_duration.get(guild_id, 0.0), size=30)
                embed = discord.Embed(title=f"🎶 Lecture en cours : {current_audio[guild_id]}", color=0x1DB954)
                embed.description = f"{bar}\n`{human_time(elapsed)} / {human_time(current_duration.get(guild_id, 0.0))}`"
                embed.set_footer(text=f"Temps total restant file: {human_time(total_remaining)}")
                await message.edit(content=None, embed=embed)
            except Exception:
                continue

# ─── ON READY ───────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    try:
        await tree.sync()
    except Exception:
        pass
    print(f"✅ Bot prêt : {bot.user} — {len(tree.get_commands())} commandes sync")
    bot.loop.create_task(nowplaying_updater())
    bot.loop.create_task(restore_all_queues_and_maybe_join())

# ─── BASIC COMMANDS (per guild) ─────────────────────────────────────────
@tree.command(name="join", description="Rejoint ton salon vocal")
async def join(interaction: discord.Interaction):
    if not interaction.guild:
        await safe_reply(interaction, "❌ Commande disponible seulement en guild.", ephemeral=True)
        return
    guild_id = interaction.guild.id
    ensure_guild_state(guild_id)
    if interaction.user.voice and interaction.user.voice.channel:
        channel = interaction.user.voice.channel
        await safe_reply(interaction, "⏳ Connexion au salon...", ephemeral=True)
        try:
            await channel.connect()
            save_queue_for_guild(guild_id)
            await safe_reply(interaction, f"✅ Connecté à {channel.name}", ephemeral=True)
            # if queue exists and not playing -> start
            if queues.get(guild_id) and not is_playing.get(guild_id, False):
                await play_next(guild_id)
        except Exception as e:
            await safe_reply(interaction, f"❌ Erreur connexion : {e}", ephemeral=True)
    else:
        await safe_reply(interaction, "❌ Tu dois être dans un salon vocal.", ephemeral=True)

@tree.command(name="leave", description="Déconnecte le bot du salon vocal")
async def leave(interaction: discord.Interaction):
    if not interaction.guild:
        await safe_reply(interaction, "❌ Commande disponible seulement en guild.", ephemeral=True)
        return
    guild_id = interaction.guild.id
    ensure_guild_state(guild_id)
    await safe_reply(interaction, "⏳ Déconnexion...", ephemeral=True)
    guild = interaction.guild
    if guild.voice_client and guild.voice_client.is_connected():
        try:
            await guild.voice_client.disconnect()
        except Exception:
            pass
        save_queue_for_guild(guild_id)
        await safe_reply(interaction, "👋 Déconnecté.", ephemeral=True)
    else:
        await safe_reply(interaction, "❌ Le bot n'est pas connecté.", ephemeral=True)

@tree.command(name="upload", description="Upload un fichier audio (formats pris en charge: mp3,wav,ogg,m4a)")
async def upload(interaction: discord.Interaction, fichier: discord.Attachment):
    if not fichier.filename.lower().endswith(AUDIO_EXTS):
        await safe_reply(interaction, "❌ Format non supporté.", ephemeral=True)
        return
    await safe_reply(interaction, "⏳ Téléchargement...", ephemeral=True)
    path = os.path.join(AUDIO_DIR, fichier.filename)
    try:
        async with aiofiles.open(path, "wb") as f:
            await f.write(await fichier.read())
        await safe_reply(interaction, f"✅ Fichier **{fichier.filename}** ajouté.")
    except Exception as e:
        await safe_reply(interaction, f"❌ Erreur upload : {e}", ephemeral=True)

@tree.command(name="list", description="Liste les fichiers audio disponibles (durée totale incluse)")
async def list_audio(interaction: discord.Interaction):
    await safe_reply(interaction, "⏳ Calcul des durées...", ephemeral=True)
    files = list_audio_files()
    if not files:
        await safe_reply(interaction, "🎵 Aucun fichier trouvé.")
        return
    total = await total_duration_of_files(files)
    lines = [f"**🎶 {len(files)} fichiers — Durée totale : `{human_time(total)}`**\n"]
    for f in files:
        d = await probe_duration(os.path.join(AUDIO_DIR, f))
        lines.append(f"• `{f}` — `{human_time(d)}`")
    msg = "\n".join(lines)
    await safe_reply(interaction, msg)

@tree.command(name="play", description="Joue un fichier audio local (nom exact ou autocomplétion)")
@app_commands.describe(nom="Nom du fichier dans audio/")
async def play(interaction: discord.Interaction, nom: str):
    if not interaction.guild:
        await safe_reply(interaction, "❌ Commande disponible seulement en guild.", ephemeral=True)
        return
    guild_id = interaction.guild.id
    ensure_guild_state(guild_id)
    path = os.path.join(AUDIO_DIR, nom)
    if not os.path.exists(path):
        await safe_reply(interaction, "❌ Fichier introuvable.", ephemeral=True)
        return
    if not interaction.user.voice or not interaction.user.voice.channel:
        await safe_reply(interaction, "❌ Tu dois être dans un salon vocal.", ephemeral=True)
        return
    await safe_reply(interaction, "⏳ Mise en file...", ephemeral=True)
    guild = interaction.guild
    # connect if needed
    if not guild.voice_client or not guild.voice_client.is_connected():
        try:
            await interaction.user.voice.channel.connect()
        except Exception as e:
            await safe_reply(interaction, f"❌ Impossible de rejoindre : {e}", ephemeral=True)
            return
    queues[guild_id].append(nom)
    save_queue_for_guild(guild_id)
    await interaction.followup.send(f"🎧 Ajouté à la file d’attente : `{nom}`")
    if not is_playing.get(guild_id, False):
        await play_next(guild_id)

@tree.command(name="playall", description="Joue tout le dossier audio ou une playlist locale (usage: /playall playlist:<name>)")
@app_commands.describe(playlist="Nom de la playlist locale (optionnel)")
async def playall(interaction: discord.Interaction, playlist: Optional[str] = None):
    if not interaction.guild:
        await safe_reply(interaction, "❌ Commande disponible seulement en guild.", ephemeral=True)
        return
    gid = interaction.guild.id
    ensure_guild_state(gid)
    await safe_reply(interaction, "⏳ Préparation...", ephemeral=True)
    files_to_add = []
    if playlist:
        pls = load_playlists_for_guild(gid)
        if playlist not in pls:
            await safe_reply(interaction, f"❌ Playlist `{playlist}` introuvable.", ephemeral=True)
            return
        files_to_add = pls[playlist].copy()
    else:
        files_to_add = list_audio_files()
    if not files_to_add:
        await safe_reply(interaction, "❌ Aucun fichier à jouer.", ephemeral=True)
        return
    # connect if needed
    if not interaction.guild.voice_client or not interaction.guild.voice_client.is_connected():
        if not interaction.user.voice or not interaction.user.voice.channel:
            await safe_reply(interaction, "❌ Tu dois être dans un salon vocal pour utiliser /playall.", ephemeral=True)
            return
        try:
            await interaction.user.voice.channel.connect()
        except Exception as e:
            await safe_reply(interaction, f"❌ Impossible de rejoindre : {e}", ephemeral=True)
            return
    added = 0
    for f in files_to_add:
        if os.path.exists(os.path.join(AUDIO_DIR, f)):
            queues[gid].append(f)
            added += 1
    save_queue_for_guild(gid)
    await interaction.followup.send(f"▶️ {added} fichiers ajoutés à la file d'attente.")
    if not is_playing.get(gid, False):
        await play_next(gid)

@tree.command(name="pause", description="Met la musique en pause")
async def pause(interaction: discord.Interaction):
    if not interaction.guild:
        await safe_reply(interaction, "❌ Commande disponible seulement en guild.", ephemeral=True)
        return
    guild = interaction.guild
    await safe_reply(interaction, "⏳ Pause...", ephemeral=True)
    if guild.voice_client and guild.voice_client.is_playing():
        guild.voice_client.pause()
        await safe_reply(interaction, "⏸️ Musique mise en pause.")
    else:
        await safe_reply(interaction, "❌ Aucune musique en cours.", ephemeral=True)

@tree.command(name="resume", description="Reprend la musique")
async def resume(interaction: discord.Interaction):
    if not interaction.guild:
        await safe_reply(interaction, "❌ Commande disponible seulement en guild.", ephemeral=True)
        return
    guild = interaction.guild
    await safe_reply(interaction, "⏳ Reprise...", ephemeral=True)
    if guild.voice_client and guild.voice_client.is_paused():
        guild.voice_client.resume()
        await safe_reply(interaction, "▶️ Musique reprise.")
    else:
        await safe_reply(interaction, "❌ Aucune musique en pause.", ephemeral=True)

@tree.command(name="skip", description="Passe à la musique suivante")
async def skip(interaction: discord.Interaction):
    if not interaction.guild:
        await safe_reply(interaction, "❌ Commande disponible seulement en guild.", ephemeral=True)
        return
    gid = interaction.guild.id
    await safe_reply(interaction, "⏳ Passage...", ephemeral=True)
    guild = interaction.guild
    if guild.voice_client and guild.voice_client.is_playing():
        guild.voice_client.stop()
        await safe_reply(interaction, "⏭️ Musique passée.")
    else:
        if queues.get(gid):
            await play_next(gid)
            await safe_reply(interaction, "⏭️ Musique passée (lecture lancée).")
        else:
            await safe_reply(interaction, "❌ Rien à passer.", ephemeral=True)

@tree.command(name="stop", description="Arrête la lecture et vide la file")
async def stop(interaction: discord.Interaction):
    if not interaction.guild:
        await safe_reply(interaction, "❌ Commande disponible seulement en guild.", ephemeral=True)
        return
    gid = interaction.guild.id
    ensure_guild_state(gid)
    await safe_reply(interaction, "⏳ Arrêt en cours...", ephemeral=True)
    queues[gid].clear()
    guild = interaction.guild
    if guild.voice_client and (guild.voice_client.is_playing() or guild.voice_client.is_paused()):
        guild.voice_client.stop()
    is_playing[gid] = False
    save_queue_for_guild(gid)
    await safe_reply(interaction, "⛔ Lecture arrêtée et file vidée.")

@tree.command(name="queue", description="Affiche la file d’attente et la durée totale restante")
async def show_queue(interaction: discord.Interaction):
    if not interaction.guild:
        await safe_reply(interaction, "❌ Commande disponible seulement en guild.", ephemeral=True)
        return
    gid = interaction.guild.id
    ensure_guild_state(gid)
    await safe_reply(interaction, "⏳ Récupération de la file...", ephemeral=True)
    if not queues.get(gid) and not current_audio.get(gid):
        await safe_reply(interaction, "🕳️ La file est vide.")
        return
    lines = []
    idx = 1
    for s in queues.get(gid, []):
        lines.append(f"{idx}. {s}")
        idx += 1
    remaining_current = 0.0
    if current_audio.get(gid) and is_playing.get(gid, False):
        elapsed = asyncio.get_event_loop().time() - (current_start.get(gid) or 0)
        remaining_current = max(0.0, current_duration.get(gid, 0.0) - elapsed)
    queue_dur = await total_duration_of_files(queues.get(gid, [])) if queues.get(gid) else 0.0
    total_remaining = remaining_current + queue_dur
    msg = "**🎵 File d’attente :**\n" + ("\n".join(lines) if lines else "(rien dans la file)") + f"\n\n⏱️ Temps total restant : `{human_time(total_remaining)}`"
    await safe_reply(interaction, msg)

@tree.command(name="nowplaying", description="Affiche la musique en cours (message mis à jour toutes les 2s)")
async def nowplaying(interaction: discord.Interaction):
    if not interaction.guild:
        await safe_reply(interaction, "❌ Commande disponible seulement en guild.", ephemeral=True)
        return
    gid = interaction.guild.id
    ensure_guild_state(gid)
    await safe_reply(interaction, "⏳ Chargement nowplaying...", ephemeral=True)
    if not current_audio.get(gid) or not is_playing.get(gid, False):
        await interaction.followup.send("❌ Aucune musique en cours.")
        return
    elapsed = asyncio.get_event_loop().time() - (current_start.get(gid) or 0)
    remaining_current = max(0.0, current_duration.get(gid, 0.0) - elapsed)
    queue_dur = await total_duration_of_files(queues.get(gid, [])) if queues.get(gid) else 0.0
    total_remaining = remaining_current + queue_dur
    bar = progress_bar(elapsed, current_duration.get(gid, 0.0), size=30)
    embed = discord.Embed(title=f"🎶 Lecture en cours : {current_audio[gid]}", color=0x1DB954)
    embed.description = f"{bar}\n`{human_time(elapsed)} / {human_time(current_duration.get(gid, 0.0))}`"
    embed.set_footer(text=f"Temps total restant file: {human_time(total_remaining)}")
    sent = await interaction.followup.send(embed=embed)
    nowplaying_messages[gid] = (sent.channel.id, sent.id)

# ─── PLAYLIST COMMANDS (local per guild) ─────────────────────────────────
playlist_group = app_commands.Group(name="playlist", description="Gestion des playlists locales (par serveur)")

async def playlist_name_autocomplete(interaction: discord.Interaction, current: str):
    if not interaction.guild:
        return []
    gid = interaction.guild.id
    pls = load_playlists_for_guild(gid)
    choices = []
    for name in pls.keys():
        if current.lower() in name.lower():
            choices.append(app_commands.Choice(name=name, value=name))
    return choices[:25]

# playlist helpers
def playlist_file(guild_id: int) -> str:
    return json_path_playlists(guild_id)

def load_playlists_for_guild(guild_id: int) -> Dict[str, List[str]]:
    return load_json(playlist_file(guild_id), {})

def save_playlists_for_guild(guild_id: int, data: Dict[str, List[str]]):
    save_json(playlist_file(guild_id), data)

@playlist_group.command(name="create", description="Crée une playlist locale pour ce serveur")
async def pl_create(interaction: discord.Interaction, name: str):
    if not interaction.guild:
        await safe_reply(interaction, "❌ Commande disponible seulement en guild.", ephemeral=True)
        return
    gid = interaction.guild.id
    pls = load_playlists_for_guild(gid)
    await safe_reply(interaction, "⏳ Création...", ephemeral=True)
    if name in pls:
        await safe_reply(interaction, f"❌ La playlist `{name}` existe déjà.", ephemeral=True)
        return
    pls[name] = []
    save_playlists_for_guild(gid, pls)
    await safe_reply(interaction, f"✅ Playlist `{name}` créée.")

@playlist_group.command(name="add", description="Ajoute un fichier local (audio/) à une playlist locale")
@app_commands.describe(name="Nom de la playlist", fichier="Nom du fichier (dans audio/)")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def pl_add(interaction: discord.Interaction, name: str, fichier: str):
    if not interaction.guild:
        await safe_reply(interaction, "❌ Commande disponible seulement en guild.", ephemeral=True)
        return
    gid = interaction.guild.id
    pls = load_playlists_for_guild(gid)
    await safe_reply(interaction, "⏳ Ajout...", ephemeral=True)
    if name not in pls:
        await safe_reply(interaction, f"❌ Playlist `{name}` introuvable.", ephemeral=True)
        return
    if not os.path.exists(os.path.join(AUDIO_DIR, fichier)):
        await safe_reply(interaction, f"❌ Le fichier `{fichier}` n'existe pas dans {AUDIO_DIR}.", ephemeral=True)
        return
    pls[name].append(fichier)
    save_playlists_for_guild(gid, pls)
    await safe_reply(interaction, f"✅ `{fichier}` ajouté à `{name}`.")

@playlist_group.command(name="remove", description="Supprime un fichier d'une playlist locale")
@app_commands.describe(name="Nom de la playlist", fichier="Nom du fichier")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def pl_remove(interaction: discord.Interaction, name: str, fichier: str):
    if not interaction.guild:
        await safe_reply(interaction, "❌ Commande disponible seulement en guild.", ephemeral=True)
        return
    gid = interaction.guild.id
    pls = load_playlists_for_guild(gid)
    await safe_reply(interaction, "⏳ Suppression...", ephemeral=True)
    if name not in pls:
        await safe_reply(interaction, f"❌ Playlist `{name}` introuvable.", ephemeral=True)
        return
    if fichier not in pls[name]:
        await safe_reply(interaction, f"❌ `{fichier}` n'est pas dans `{name}`.", ephemeral=True)
        return
    pls[name].remove(fichier)
    save_playlists_for_guild(gid, pls)
    await safe_reply(interaction, f"✅ `{fichier}` supprimé de `{name}`.")

@playlist_group.command(name="list", description="Liste les playlists locales et leur contenu")
async def pl_list(interaction: discord.Interaction):
    if not interaction.guild:
        await safe_reply(interaction, "❌ Commande disponible seulement en guild.", ephemeral=True)
        return
    gid = interaction.guild.id
    pls = load_playlists_for_guild(gid)
    await safe_reply(interaction, "⏳ Récupération playlists...", ephemeral=True)
    if not pls:
        await safe_reply(interaction, "📂 Aucune playlist locale enregistrée.")
        return
    lines = []
    for name, items in pls.items():
        total = await total_duration_of_files(items) if items else 0.0
        lines.append(f"• `{name}` — {len(items)} fichiers — Durée totale : `{human_time(total)}`")
        for it in items:
            lines.append(f"    • `{it}`")
    await safe_reply(interaction, "\n".join(lines))

@playlist_group.command(name="delete", description="Supprime une playlist locale")
@app_commands.autocomplete(name=playlist_name_autocomplete)
async def pl_delete(interaction: discord.Interaction, name: str):
    if not interaction.guild:
        await safe_reply(interaction, "❌ Commande disponible seulement en guild.", ephemeral=True)
        return
    gid = interaction.guild.id
    pls = load_playlists_for_guild(gid)
    await safe_reply(interaction, "⏳ Suppression...", ephemeral=True)
    if name not in pls:
        await safe_reply(interaction, f"❌ Playlist `{name}` introuvable.", ephemeral=True)
        return
    pls.pop(name)
    save_playlists_for_guild(gid, pls)
    await safe_reply(interaction, f"✅ Playlist `{name}` supprimée.")

tree.add_command(playlist_group)

# ─── SPAM (small utility) ───────────────────────────────────────────────
@tree.command(name="spam", description="📣 Envoie plusieurs messages rapidement (max 10)")
async def spam(interaction: discord.Interaction, message: str, nombre: int):
    if nombre > 10:
        await safe_reply(interaction, "⚠️ Max 10 messages.", ephemeral=True)
        return
    await safe_reply(interaction, f"💬 Envoi de {nombre} messages :", ephemeral=True)
    for _ in range(nombre):
        # send to channel directly (not ephemeral)
        await interaction.channel.send(message)

# ─── SHUTDOWN HANDLERS ──────────────────────────────────────────────────
@bot.event
async def on_disconnect():
    save_all_state()

@bot.event
async def on_close():
    save_all_state()

# ─── START BOT ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        print("Error: TOKEN not set in .env")
        raise SystemExit(1)
    bot.run(TOKEN)
