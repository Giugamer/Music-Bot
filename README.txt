🎵 DISCORD BOT - "SPOTIFY++"

A complete and stylish Spotify-inspired Discord music bot that can:

Play, pause, resume, skip, and stop audio files 🎧

Manage a dynamic music queue with live progress bars ⏱️

Create, load, and delete playlists 💾

Display all available audio files 📂

Upload new songs directly from Discord 📤

Spam fun messages (safe limit: 10 per command) 📣

Clean Spotify-like embeds and interactive control buttons 🟢

⚙️ INSTALLATION
1️⃣ Requirements

Windows 10/11, Linux, or macOS

Python 3.10+ (3.12+ recommended)

FFmpeg installed and added to your PATH

Download FFmpeg:
👉 https://ffmpeg.org/download.html

or install via winget:

winget install ffmpeg

2️⃣ Install dependencies

In your bot’s folder, run:

pip install -r requirements.txt


If pip isn’t recognized:

python -m pip install -r requirements.txt

3️⃣ In the .env file

Replace `your_discord_bot_token_here` with your token.:

⚠️ Never share your Discord token publicly!
You can find it on the Discord Developer Portal
 → Bot → Token.

4️⃣ Start your bot

Run the following command:

python bot.py

🎮 SLASH COMMANDS
Command	Description
/play [name]	Play an audio file from the /audio folder
/pause	Pause the current track
/resume	Resume playback
/skip	Skip the current song
/stop	Stop playback and clear the queue
/queue	Display the current queue
/clearqueue	Clear the queue
/nowplaying	Show the currently playing song with progress
/list	List all available audio files with duration
/upload	Upload an audio file directly from Discord
/playlist_create	Create a new playlist
/playlist_add	Add a song to a playlist
/playlist_list	Show all saved playlists
/playlist_load	Load a playlist into the queue
/playlist_delete	Delete a playlist
/spam	Send multiple messages quickly (limit 10)
🗂️ FOLDER STRUCTURE
📁 your_bot_folder/
│
├── bot.py               ← Main bot code
├── requirements.txt      ← List of dependencies
├── .env                  ← Your Discord bot token
├── README.txt            ← This file (English)
├── LISEZ_MOI.txt         ← French version
│
├── 📁 audio/              ← Your music files (MP3, WAV, etc.)
└── 📁 playlists/          ← JSON playlists created by the bot

💡 TIPS

All audio files should be placed in the /audio folder.

The bot automatically creates /audio and /playlists if they don’t exist.

Supported formats: .mp3, .wav, .ogg, .flac.

If slash commands don’t appear, type / in Discord to refresh them.

🚀 BOT DETAILS

Name: Spotify++

Language: Python 3

Library: discord.py

Version: 2.5

Author: [@giugamer]