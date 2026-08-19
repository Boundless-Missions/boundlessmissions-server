"""
config.py – Centralised environment-variable loader.
All secrets are read from the .env file (or the real environment).
Import `cfg` anywhere in the project to access settings.
"""

import os
import logging
from dotenv import load_dotenv

# Load .env from the project root (one directory up from this file)
load_dotenv()


def _require(key: str) -> str:
    """Read a required env var; raise if missing."""
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"Missing required environment variable: {key}\n"
            f"Check your .env file against .env.example"
        )
    return value


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default)


class Config:
    # ── Discord credentials ─────────────────────
    TOKEN: str = _require("DISCORD_TOKEN")
    CLIENT_ID: str = _optional("DISCORD_CLIENT_ID")
    CLIENT_SECRET: str = _optional("DISCORD_CLIENT_SECRET")

    # ── Guild IDs for dev slash-command sync ───
    # e.g. "123456789,987654321"  → [123456789, 987654321]
    _raw_guilds = _optional("GUILD_IDS", "")
    GUILD_IDS: list[int] = (
        [int(g.strip()) for g in _raw_guilds.split(",") if g.strip()]
        if _raw_guilds
        else []
    )

    # ── General settings ────────────────────────
    COMMAND_PREFIX: str = _optional("COMMAND_PREFIX", "!")
    OWNER_ID: int = int(_optional("BOT_OWNER_ID", "0") or "0")

    # ── Slash command group ──────────────────────
    # If set, all slash commands live under this group name.
    # e.g. COMMAND_GROUP=gk  →  /gk help, /gk ping, /gk kick …
    # Leave blank to keep bare top-level commands (/help, /ping …)
    COMMAND_GROUP: str = _optional("COMMAND_GROUP", "")

    # ── Feature flags ───────────────────────────
    # Set to "false" (case-insensitive) to disable the moderation cog entirely
    ENABLE_MOD_COMMANDS: bool = _optional("ENABLE_MOD_COMMANDS", "true").lower() not in ("false", "0", "no", "off")

    # ── KSP API Server ──────────────────────────
    KSP_API_ENABLED: bool = _optional("KSP_API_ENABLED", "true").lower() not in ("false", "0", "no", "off")
    API_HOST: str = _optional("API_HOST", "0.0.0.0")
    API_PORT: int = int(_optional("API_PORT", "5022"))
    API_SECRET_KEY: str = _optional("API_SECRET_KEY", "")

    # Interactive API docs (Swagger UI + OpenAPI schema). Off by default: the docs
    # enumerate every endpoint for an attacker and the KSP client never needs them.
    # Set API_DOCS_ENABLED=true only for local development.
    API_DOCS_ENABLED: bool = _optional("API_DOCS_ENABLED", "false").lower() in ("true", "1", "yes", "on")

    # Debug/test-only endpoints (e.g. /api/v1/debug/signtest, used by the KSP debug
    # test panel to verify the signed-URL invariant). OFF by default and 404 when
    # off — invisible in production, exactly like the owner console. Turn on ONLY on
    # a dev server with DEBUG_ENDPOINTS_ENABLED=true to run the in-game live tests.
    DEBUG_ENDPOINTS_ENABLED: bool = _optional("DEBUG_ENDPOINTS_ENABLED", "false").lower() in ("true", "1", "yes", "on")

    # Browser CORS allow-list (comma-separated origins). Empty by default — the KSP
    # client is UnityWebRequest (not a browser) and needs no CORS, so no wildcard is
    # served. Set explicit origins only if a browser front-end must call the API.
    _raw_cors = _optional("API_CORS_ORIGINS", "")
    API_CORS_ORIGINS: list[str] = [o.strip() for o in _raw_cors.split(",") if o.strip()]

    # Discord DM login approval for KSP account linking. When on, a valid link
    # code only earns a "push approval" prompt DM'd to the user, who must press a
    # Log-in button in Discord before a token is issued. Default on (secure); set
    # KSP_2FA_ENABLED=false in .env to skip it while testing.
    KSP_2FA_ENABLED: bool = _optional("KSP_2FA_ENABLED", "true").lower() not in ("false", "0", "no", "off")

    # Device binding for KSP linking. When on, each install's random device id is
    # bound to the account at link time and any *other* device (e.g. a copied
    # session token) is hard-blocked until the user approves it from Discord.
    # Default on (secure); set KSP_DEVICE_BINDING_ENABLED=false in .env to disable.
    KSP_DEVICE_BINDING_ENABLED: bool = _optional("KSP_DEVICE_BINDING_ENABLED", "true").lower() not in ("false", "0", "no", "off")

    # Mod version gate. When on, the KSP client reports its DLL's SHA256 and the
    # server compares it against the published latest hash (see /admin publishversion
    # and config/mod_version in Firestore); an outdated client is blocked in-game
    # until the player updates. Default on; set KSP_VERSION_CHECK_ENABLED=false in
    # .env to disable the gate (clients are then never told to update). When no
    # version has been published yet, the gate never blocks regardless of this flag.
    KSP_VERSION_CHECK_ENABLED: bool = _optional("KSP_VERSION_CHECK_ENABLED", "true").lower() not in ("false", "0", "no", "off")

    # IPs of trusted reverse proxies (comma-separated, e.g. "127.0.0.1"). When a
    # request's direct peer is one of these, the real client IP is read from
    # X-Forwarded-For for rate limiting. Leave empty when clients connect the API
    # directly — the header is attacker-controlled and is ignored unless the peer
    # is a configured proxy.
    _raw_proxies = _optional("API_TRUSTED_PROXIES", "")
    API_TRUSTED_PROXIES: set[str] = {p.strip() for p in _raw_proxies.split(",") if p.strip()}

    # Optional direct TLS for the in-process API server. Set BOTH to serve HTTPS
    # straight from uvicorn (no proxy). Leave empty when terminating TLS at a
    # reverse proxy (the recommended setup) or on localhost.
    API_SSL_CERTFILE: str = _optional("API_SSL_CERTFILE", "")
    API_SSL_KEYFILE: str = _optional("API_SSL_KEYFILE", "")

    # ── Firebase / Firestore ────────────────────
    # Path to the Firebase service account JSON key file
    FIREBASE_CREDENTIALS: str = _require("FIREBASE_CREDENTIALS")

    # ── Logging ─────────────────────────────────
    LOG_LEVEL: str = _optional("LOG_LEVEL", "INFO").upper()


cfg = Config()

# The API secret signs every KSP session token. A blank or placeholder value
# means the signing key is publicly known, letting anyone forge a token for any
# user — so refuse to start with one (unless the KSP API is disabled entirely).
_DEFAULT_API_SECRETS = {
    "", "gk-change-this-secret-key", "gk-default-secret-change-me",
    "your_random_secret_here",
}
if cfg.KSP_API_ENABLED and cfg.API_SECRET_KEY.strip() in _DEFAULT_API_SECRETS:
    raise EnvironmentError(
        "API_SECRET_KEY is unset or still a default placeholder. It signs KSP "
        "session tokens; with a known value anyone can forge a token for any "
        "user. Set a strong random value in .env, e.g.:\n"
        "    python -c \"import secrets; print(secrets.token_urlsafe(48))\"\n"
        "Or disable the KSP API with KSP_API_ENABLED=false."
    )

# Configure root logger once here so every module inherits it
logging.basicConfig(
    level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
