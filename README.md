# Discord Bot – Gene Kerman

A modular, non-production-ready Discord bot built with **discord.py 2.x** designed for Kerbal Space Program (KSP) communities. Features a fully-fledged economy, corporation system, contracts, weekly missions, and AI-powered screenshot analysis.

## Features

| Category | Description / Commands |
|---|---|
| **Contracts** | Player-to-player and AI contract system (`/contract create`, `/contracts`) |
| **Weekly Missions** | AI-generated weekly challenges with difficulty tiers |
| **Corporations** | Create and manage corps, private channels (`/g corpsetup`, `/g corp`) |
| **Economy** | KCoins currency, balances, payments (`/balance`, `/pay`, `/leaderboard`) |
| **XP System** | Leveling system with rewards for chatting and completing missions |
| **Screenshots** | AI analysis of KSP screenshots using Gemini AI to detect celestial bodies and mods (`/analyze`) |
| **GKChannels** | Gate bot commands to specific channels (`/mod gkchannel`) |
| **Admin/Mod** | General moderation, announcements, localization settings, and configuration |

All commands are available as **slash commands** (`/`).

---

## Quick Start

### 1. Clone / open the project

```bash
git clone https://github.com/KSPRehber/UPoK-DCBot.git genekerman
cd genekerman
```

### 2. Create a virtual environment & install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment variables and settings

```bash
cp .env.example .env
nano .env   # fill in your values
```

You must configure:
- `DISCORD_TOKEN`: Bot token from [Discord Developer Portal](https://discord.com/developers/applications)
- Firebase Admin SDK credentials JSON (for Firestore database)
- `GEMINI_API_KEY`: For AI contract reviews and screenshot analysis

**Note:** Economy rates, cooldowns, XP curves, and feature toggles (like allowing mods to bypass weekly mission locks) can be safely adjusted in `settings.py`.

### 4. Run the bot

```bash
# Don't forget to activate the virtual environment!
source .venv/bin/activate
python bot.py --sync
```

---

## Project Structure

```
genekerman/
├── bot.py                # Entry point: loads cogs, syncs commands, starts bot
├── config.py             # Env-var loader
├── settings.py           # Tunable gameplay/economy variables
├── requirements.txt
├── .env.example          # Template – copy to .env
├── .env                  # Your secrets (gitignored)
├── data/                 # Firestore integration & models
├── i18n.py               # Localization (TR/EN)
└── cogs/
    ├── contracts.py      # Contract system logic
    ├── contract_views.py # Interactive UI for contracts
    ├── corps.py          # Corporation management
    ├── economy.py        # KCoins & transactions
    ├── weeklymissions.py # Automated weekly missions board
    ├── screenshots.py    # Gemini AI screenshot analyzer
    ├── xp.py             # Leveling system
    ├── gkchannels.py     # Channel restrictions
    ├── general.py        # Help, ping
    ├── admin.py          # Admin commands
    ├── moderation.py     # Moderation tools
    └── info.py           # Server info
```

## Security & Architecture Notes

- `.env` and Firebase credentials are **never** committed — ensure they are in `.gitignore`.
- All persistent data (balances, users, contracts, guilds) is stored in **Firebase Firestore**.
- Mod-only commands are permission-gated via Discord's native Role/Permissions system.
