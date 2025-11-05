# -------------------- PARTIE 1/2 - Spotify++ (config, utilitaires, playback, UI, updater) --------------------
import os
import time
import random
import asyncio
import logging
import json
from typing import List, Dict, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from mutagen import File as MutagenFile

# ---------- CONFIG ----------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
# Use project folder 'audio' and 'playlists' next to bot.py
BASE_DIR = os.getcwd()
AUDIO_DIR = os.path.join(BASE_DIR, "audio")
PLAYLIST_DIR = os.path.join(BASE_DIR, "playlists")
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(PLAYLIST_DIR, exist_ok=True)

FFMPEG_OPTIONS = {"options": "-vn"}
LOG_LEVEL = logging.INFO

logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] [%(levelname)8s] %(message)s")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- GLOBAL STATE ----------
guild_queues: Dict[int, List[Tuple[str, str, Optional[str]]]] = {}
guild_playlists: Dict[int, Dict[str, List[str]]] = {}
now_playing_info: Dict[int, Dict] = {}
nowplaying_tasks: Dict[int, asyncio.Task] = {}

# Settings
SPOTIFY_GREEN = 0x1DB954
SPOTIFY_YELLOW = 0xD6A200
SPOTIFY_RED = 0xE0245E
PROGRESS_BAR_LENGTH = 22
TITLE_ANIM = ["🎵", "🎶", "🎧"]

# ---------- UTILITIES ----------
def get_queue(guild_id: int):
    return guild_queues.setdefault(guild_id, [])

def get_playlists(guild_id: int):
    return guild_playlists.setdefault(guild_id, {})

def save_playlist(guild_id: int, name: str, files: list):
    folder = os.path.join(PLAYLIST_DIR, str(guild_id))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(files, f, ensure_ascii=False, indent=2)

def load_playlists(guild_id: int) -> Dict[str, list]:
    folder = os.path.join(PLAYLIST_DIR, str(guild_id))
    playlists = {}
    if not os.path.exists(folder):
        return playlists
    for fn in os.listdir(folder):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(folder, fn), "r", encoding="utf-8") as f:
                    playlists[fn[:-5]] = json.load(f)
            except Exception:
                logging.exception("Failed loading playlist %s", fn)
    return playlists

def human_time(seconds: int) -> str:
    seconds = int(max(0, seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def get_audio_duration(path: str) -> int:
    try:
        m = MutagenFile(path)
        if m and m.info and getattr(m.info, "length", None):
            return int(m.info.length)
    except Exception:
        logging.debug("Mutagen failed for %s", path)
    return 0

def progress_bar(elapsed: int, duration: int, length: int = PROGRESS_BAR_LENGTH) -> str:
    if duration <= 0:
        return "▫️" * length
    pos = int((elapsed / duration) * length)
    pos = max(0, min(pos, length))
    filled = "█" * pos
    empty = "─" * (length - pos)
    # Use block style for clearer bar inside embed code block
    return f"[{filled}{empty}]"

def embed_color_for_state(paused: bool, playing: bool) -> int:
    if playing and not paused:
        return SPOTIFY_GREEN
    if paused:
        return SPOTIFY_YELLOW
    return SPOTIFY_RED

# ---------- NowPlaying embed & View ----------
def build_nowplaying_embed(guild_id: int, title_anim_index: int = 0) -> discord.Embed:
    info = now_playing_info.get(guild_id)
    if not info:
        embed = discord.Embed(title="Aucune lecture", description="Utilise `/play` pour démarrer une piste.", color=SPOTIFY_RED)
        return embed

    title = info.get("title", "Inconnu")
    duration = info.get("duration", 0)
    start_time = info.get("start_time", time.time())
    paused = info.get("paused", False)
    pause_time = info.get("pause_time")
    # If paused, compute elapsed from pause_time if present
    if paused and pause_time:
        elapsed = int(pause_time - start_time)
    else:
        elapsed = int(time.time() - start_time)
    elapsed = max(0, min(elapsed, duration))
    bar = progress_bar(elapsed, duration)
    anim = TITLE_ANIM[title_anim_index % len(TITLE_ANIM)]
    embed = discord.Embed(title=f"{anim} Lecture en cours", color=embed_color_for_state(paused, (not paused and duration>0)))
    embed.add_field(name="Titre", value=title, inline=False)
    embed.add_field(name="Progression", value=f"`{human_time(elapsed)} / {human_time(duration)}`\n{bar}", inline=False)
    embed.set_footer(text="⏯️ Play/Pause • ⏭️ Skip • 🔁 Loop • ⏹️ Stop")
    return embed

class NowPlayingView(discord.ui.View):
    def __init__(self, guild_id: int, owner_id: Optional[int] = None, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Allow everyone by default; could restrict to controller or role if needed
        return True

    @discord.ui.button(label="⏯️", style=discord.ButtonStyle.blurple)
    async def playpause(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc:
            return await interaction.response.send_message("❌ Le bot n'est pas connecté au salon vocal.", ephemeral=True)
        if vc.is_playing():
            vc.pause()
            info = now_playing_info.get(interaction.guild.id)
            if info:
                info["paused"] = True
                info["pause_time"] = time.time()
            await interaction.response.send_message("⏸️ Lecture mise en pause.", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            info = now_playing_info.get(interaction.guild.id)
            if info and info.get("pause_time"):
                paused_for = time.time() - info["pause_time"]
                info["start_time"] = info.get("start_time", time.time()) + paused_for
                info.pop("pause_time", None)
                info["paused"] = False
            await interaction.response.send_message("▶️ Lecture reprise.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Rien à jouer.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await interaction.response.send_message("⏭️ Musique passée.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Rien n'est joué.", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary)
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        info = now_playing_info.setdefault(interaction.guild.id, {})
        info["loop"] = not info.get("loop", False)
        await interaction.response.send_message(f"🔁 Loop {'activé' if info['loop'] else 'désactivé'}.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
            try:
                await vc.disconnect()
            except Exception:
                pass
        q = get_queue(interaction.guild.id)
        for _, _, tmp in q:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
        q.clear()
        now_playing_info.pop(interaction.guild.id, None)
        task = nowplaying_tasks.pop(interaction.guild.id, None)
        if task and not task.done():
            task.cancel()
        await interaction.response.send_message("⏹️ Arrêté et queue vidée.", ephemeral=True)

# ---------- Updater task (updates np message each second & animates title) ----------
async def nowplaying_updater(guild_id: int):
    try:
        anim_index = 0
        while True:
            await asyncio.sleep(1)
            info = now_playing_info.get(guild_id)
            if not info:
                return
            msg = info.get("np_message")
            if not msg:
                # nothing to update yet
                continue
            try:
                embed = build_nowplaying_embed(guild_id, title_anim_index=anim_index)
                view = NowPlayingView(guild_id, owner_id=info.get("controller_id"))
                # edit the stored message - use try/except because message might be deleted
                await msg.edit(embed=embed, view=view)
            except Exception:
                # don't crash updater on any edit failure
                logging.debug("Failed to edit nowplaying message for guild %s", guild_id, exc_info=True)
            anim_index = (anim_index + 1) % len(TITLE_ANIM)
    except asyncio.CancelledError:
        return

# ---------- Playback handling ----------
async def play_next(guild: discord.Guild, interaction_for_context: Optional[discord.Interaction] = None):
    """Pop the next track from the queue and play it. Handles loop flag."""
    queue = get_queue(guild.id)
    if not queue:
        # stop voice and cleanup
        now_playing_info.pop(guild.id, None)
        vc = guild.voice_client
        if vc and vc.is_connected():
            try:
                await vc.disconnect()
            except Exception:
                pass
        task = nowplaying_tasks.pop(guild.id, None)
        if task and not task.done():
            task.cancel()
        return

    file_path, title, temp_file = queue.pop(0)
    # create audio source
    try:
        source = discord.FFmpegOpusAudio(file_path, **FFMPEG_OPTIONS)
    except Exception as e:
        logging.exception("FFmpeg source creation failed for %s: %s", file_path, e)
        # try next track
        return await play_next(guild, interaction_for_context)

    duration = get_audio_duration(file_path)
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        # cannot play if not connected
        return

    # preserve loop flag if present
    loop_flag = now_playing_info.get(guild.id, {}).get("loop", False)
    now_playing_info[guild.id] = {
        "file_path": file_path,
        "title": title,
        "temp_file": temp_file,
        "start_time": time.time(),
        "duration": duration,
        "loop": loop_flag,
        "np_message": now_playing_info.get(guild.id, {}).get("np_message"),
        "controller_id": now_playing_info.get(guild.id, {}).get("controller_id"),
        "paused": False
    }

    def after_play(err):
        if err:
            logging.exception("Playback error", exc_info=err)
        meta = now_playing_info.get(guild.id)
        if meta and meta.get("loop"):
            # requeue the same track at front
            get_queue(guild.id).insert(0, (meta["file_path"], meta["title"], meta["temp_file"]))
        # schedule next in event loop
        asyncio.run_coroutine_threadsafe(play_next(guild, interaction_for_context), bot.loop)

    vc.play(source, after=after_play)

    # start updater task (cancel previous if running)
    task = nowplaying_tasks.get(guild.id)
    if task and not task.done():
        task.cancel()
    nowplaying_tasks[guild.id] = bot.loop.create_task(nowplaying_updater(guild.id))

# ---------- Helper: clear queue ----------
def clear_queue(guild_id: int):
    q = get_queue(guild_id)
    for _, _, tmp in q:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
    q.clear()

# ---------- Ready event: load playlists ----------
@bot.event
async def on_ready():
    # make sure playlists are loaded for current guilds
    for g in bot.guilds:
        guild_playlists[g.id] = load_playlists(g.id)
    try:
        synced = await bot.tree.sync()
        logging.info("Bot ready: %s — %d commands synced", bot.user, len(synced))
    except Exception:
        logging.exception("Command sync failed")

# -------------------- END PARTIE 1/2 --------------------
# -------------------- PARTIE 2/2 - Spotify++ (commandes Slash & logique Discord) --------------------
import aiofiles
from discord.ext import tasks

# ---------- Commandes slash ----------

@bot.tree.command(name="play", description="🎵 Joue un fichier audio depuis le dossier /audio.")
@app_commands.describe(nom="Nom du fichier audio (ex: musique.mp3)")
async def play(interaction: discord.Interaction, nom: str):
    await interaction.response.defer(thinking=True)
    vc = interaction.guild.voice_client
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("❌ Tu dois être connecté à un salon vocal.")
    channel = interaction.user.voice.channel

    file_path = os.path.join(AUDIO_DIR, nom)
    if not os.path.exists(file_path):
        return await interaction.followup.send("❌ Fichier introuvable dans /audio.")

    if not vc or not vc.is_connected():
        vc = await channel.connect()

    q = get_queue(interaction.guild.id)
    q.append((file_path, nom, None))

    if vc.is_playing() or vc.is_paused():
        await interaction.followup.send(f"📥 **{nom}** ajouté à la file d’attente.")
    else:
        await interaction.followup.send(f"▶️ Lecture de **{nom}** lancée.")
        now_playing_info[interaction.guild.id] = {"controller_id": interaction.user.id}
        await play_next(interaction.guild, interaction)

        embed = build_nowplaying_embed(interaction.guild.id)
        view = NowPlayingView(interaction.guild.id, owner_id=interaction.user.id)
        msg = await interaction.channel.send(embed=embed, view=view)
        now_playing_info[interaction.guild.id]["np_message"] = msg


@bot.tree.command(name="pause", description="⏸️ Met la musique en pause.")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("❌ Aucune musique en cours.")
    vc.pause()
    info = now_playing_info.get(interaction.guild.id)
    if info:
        info["paused"] = True
        info["pause_time"] = time.time()
    await interaction.response.send_message("⏸️ Lecture mise en pause.")


@bot.tree.command(name="resume", description="▶️ Reprend la lecture.")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_paused():
        return await interaction.response.send_message("❌ Rien à reprendre.")
    vc.resume()
    info = now_playing_info.get(interaction.guild.id)
    if info and info.get("pause_time"):
        paused_for = time.time() - info["pause_time"]
        info["start_time"] = info.get("start_time", time.time()) + paused_for
        info.pop("pause_time", None)
        info["paused"] = False
    await interaction.response.send_message("▶️ Lecture reprise.")


@bot.tree.command(name="skip", description="⏭️ Passe à la musique suivante.")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await interaction.response.send_message("⏭️ Musique passée.")
    else:
        await interaction.response.send_message("❌ Aucune musique à passer.")


@bot.tree.command(name="stop", description="⏹️ Arrête la lecture et vide la file d’attente.")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        vc.stop()
        await vc.disconnect()
    clear_queue(interaction.guild.id)
    now_playing_info.pop(interaction.guild.id, None)
    task = nowplaying_tasks.pop(interaction.guild.id, None)
    if task and not task.done():
        task.cancel()
    await interaction.response.send_message("⏹️ Lecture arrêtée et file vidée.")


@bot.tree.command(name="nowplaying", description="🎶 Affiche la musique en cours avec progression dynamique.")
async def nowplaying(interaction: discord.Interaction):
    embed = build_nowplaying_embed(interaction.guild.id)
    view = NowPlayingView(interaction.guild.id)
    msg = await interaction.response.send_message(embed=embed, view=view)
    now_playing_info.setdefault(interaction.guild.id, {})["np_message"] = await msg.original_response()
    # démarre ou relance l’updater
    task = nowplaying_tasks.get(interaction.guild.id)
    if task and not task.done():
        task.cancel()
    nowplaying_tasks[interaction.guild.id] = bot.loop.create_task(nowplaying_updater(interaction.guild.id))


@bot.tree.command(name="queue", description="📜 Affiche la file d’attente actuelle.")
async def queue(interaction: discord.Interaction):
    q = get_queue(interaction.guild.id)
    if not q:
        return await interaction.response.send_message("🎶 La file d’attente est vide.")
    total_time = sum(get_audio_duration(f) for f, _, _ in q)
    desc = "\n".join([f"**{i+1}.** {title} ({human_time(get_audio_duration(path))})" for i, (path, title, _) in enumerate(q)])
    embed = discord.Embed(
        title="📜 File d’attente actuelle",
        description=f"{desc}\n\n⏱️ Durée totale : **{human_time(total_time)}**",
        color=SPOTIFY_GREEN,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="clearqueue", description="🧹 Vide la file d’attente.")
async def clearqueue(interaction: discord.Interaction):
    clear_queue(interaction.guild.id)
    await interaction.response.send_message("🧹 File d’attente vidée.")


@bot.tree.command(name="list", description="📁 Liste les fichiers audio disponibles.")
async def list_audio(interaction: discord.Interaction):
    files = [f for f in os.listdir(AUDIO_DIR) if os.path.isfile(os.path.join(AUDIO_DIR, f))]
    if not files:
        return await interaction.response.send_message("Aucun fichier trouvé dans /audio.")
    files.sort()
    page_size = 15
    total_pages = (len(files) - 1) // page_size + 1
    desc = ""
    for i, f in enumerate(files):
        dur = human_time(get_audio_duration(os.path.join(AUDIO_DIR, f)))
        desc += f"**{i+1}.** {f} ({dur})\n"
    embed = discord.Embed(title=f"🎵 Fichiers disponibles ({len(files)})", description=desc, color=SPOTIFY_GREEN)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="upload", description="📤 Upload un fichier audio vers le dossier /audio.")
@app_commands.describe(fichier="Le fichier audio à envoyer.")
async def upload(interaction: discord.Interaction, fichier: discord.Attachment):
    if not fichier.filename.lower().endswith((".mp3", ".wav", ".ogg", ".flac")):
        return await interaction.response.send_message("❌ Format non supporté. Formats acceptés : mp3, wav, ogg, flac.")
    dest = os.path.join(AUDIO_DIR, fichier.filename)
    await interaction.response.defer(thinking=True)
    async with aiofiles.open(dest, "wb") as f:
        await f.write(await fichier.read())
    await interaction.followup.send(f"✅ Fichier **{fichier.filename}** uploadé avec succès !")


# ---------- Gestion des playlists ----------

@bot.tree.command(name="playlist_create", description="💾 Crée une playlist vide.")
async def playlist_create(interaction: discord.Interaction, nom: str):
    pls = get_playlists(interaction.guild.id)
    if nom in pls:
        return await interaction.response.send_message("❌ Cette playlist existe déjà.")
    pls[nom] = []
    save_playlist(interaction.guild.id, nom, [])
    await interaction.response.send_message(f"💾 Playlist **{nom}** créée.")


@bot.tree.command(name="playlist_add", description="➕ Ajoute un fichier à une playlist.")
async def playlist_add(interaction: discord.Interaction, nom: str, fichier: str):
    pls = get_playlists(interaction.guild.id)
    if nom not in pls:
        return await interaction.response.send_message("❌ Playlist introuvable.")
    path = os.path.join(AUDIO_DIR, fichier)
    if not os.path.exists(path):
        return await interaction.response.send_message("❌ Fichier inexistant dans /audio.")
    pls[nom].append(fichier)
    save_playlist(interaction.guild.id, nom, pls[nom])
    await interaction.response.send_message(f"✅ **{fichier}** ajouté à la playlist **{nom}**.")


@bot.tree.command(name="playlist_list", description="🎶 Liste toutes les playlists.")
async def playlist_list(interaction: discord.Interaction):
    pls = get_playlists(interaction.guild.id)
    if not pls:
        return await interaction.response.send_message("❌ Aucune playlist enregistrée.")
    desc = "\n".join([f"• **{name}** ({len(tracks)} morceaux)" for name, tracks in pls.items()])
    embed = discord.Embed(title="🎶 Playlists disponibles", description=desc, color=SPOTIFY_GREEN)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="playlist_load", description="📂 Charge une playlist et l’ajoute à la file.")
async def playlist_load(interaction: discord.Interaction, nom: str):
    pls = get_playlists(interaction.guild.id)
    if nom not in pls:
        return await interaction.response.send_message("❌ Playlist inexistante.")
    q = get_queue(interaction.guild.id)
    for f in pls[nom]:
        path = os.path.join(AUDIO_DIR, f)
        if os.path.exists(path):
            q.append((path, f, None))
    await interaction.response.send_message(f"📂 Playlist **{nom}** chargée avec succès !")


@bot.tree.command(name="playlist_delete", description="🗑️ Supprime une playlist.")
async def playlist_delete(interaction: discord.Interaction, nom: str):
    pls = get_playlists(interaction.guild.id)
    if nom not in pls:
        return await interaction.response.send_message("❌ Playlist introuvable.")
    del pls[nom]
    path = os.path.join(PLAYLIST_DIR, str(interaction.guild.id), f"{nom}.json")
    if os.path.exists(path):
        os.remove(path)
    await interaction.response.send_message(f"🗑️ Playlist **{nom}** supprimée.")


# ---------- Commande spam ----------
from typing import Optional

@bot.tree.command(name="spam", description="📣 Envoie plusieurs messages rapidement.")
@app_commands.describe(
    message="Message à envoyer",
    repetitions="Nombre de répétitions (max 10)",
    delai="Délai entre chaque message (sec)"
)
async def spam(
    interaction: discord.Interaction,
    message: str,
    repetitions: Optional[float] = None,  # <-- None par défaut
    delai: Optional[float] = None         # <-- None par défaut
):
    repetitions = int(repetitions) if repetitions is not None else 5
    delai = float(delai) if delai is not None else 0.0

    if repetitions > 10:
        return await interaction.response.send_message("❌ Max 10 messages à la fois.")

    await interaction.response.send_message(f"📣 Envoi de {repetitions} messages...")
    for i in range(repetitions):
        await interaction.channel.send(f"{message} ({i+1}/{repetitions})")
        await asyncio.sleep(delai)

    await interaction.followup.send("✅ Spam terminé !", ephemeral=True)

# ---------- Lancement ----------
if __name__ == "__main__":
    if not TOKEN:
        print("❌ Erreur : Aucun token trouvé. Vérifie ton fichier .env.")
    else:
        bot.run(TOKEN)

# -------------------- FIN PARTIE 2/2 --------------------

