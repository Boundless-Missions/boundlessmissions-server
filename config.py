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
    #
    # NOT an allowlist, and nothing reads it any more. It only ever chose where
    # slash commands were *registered*, which is discoverability rather than
    # authority — it did not stop the bot being added to a server, did not cover
    # prefix or component interactions, and syncing globally when blank (the
    # shipped default) registered every command everywhere. The guilds the bot
    # will actually serve are hardcoded in `guild_gate.py`, and `_sync_commands`
    # now follows that list. Kept only so an existing .env does not fail to load.
    _raw_guilds = _optional("GUILD_IDS", "")
    GUILD_IDS: list[int] = (
        [int(g.strip()) for g in _raw_guilds.split(",") if g.strip()]
        if _raw_guilds
        else []
    )

    # ── Home guild ──────────────────────────────
    # The guild an account with no Discord of its own belongs to: where its
    # tickets are opened and which guild's config answers for it. Deliberately an
    # env var rather than a literal, so dev and prod can differ — and 0 means "not
    # configured", which every caller must treat as "no Discord surface available"
    # rather than as guild zero.
    HOME_GUILD_ID: int = int(_optional("HOME_GUILD_ID", "0") or "0")

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
    # Loopback by default. This API carries session tokens in plaintext and is
    # meant to sit behind Caddy on the same host; `0.0.0.0` put it on every
    # interface the moment the firewall was wrong, and made exposure the default
    # rather than something an operator typed. Set API_HOST=0.0.0.0 deliberately
    # if the proxy really is on another machine.
    API_HOST: str = _optional("API_HOST", "127.0.0.1")
    API_PORT: int = int(_optional("API_PORT", "5022"))
    API_SECRET_KEY: str = _optional("API_SECRET_KEY", "")
    # Previous signing key, accepted for VERIFICATION only during a rotation
    # window (new tokens are always signed with API_SECRET_KEY). To rotate: move
    # the current key here, put a fresh key in API_SECRET_KEY, restart; existing
    # sessions keep working and re-mint under the new key as they relink. Clear
    # this after 30 days (TOKEN_LIFETIME) — past that no valid token can still
    # carry the old signature anyway.
    API_SECRET_KEY_PREVIOUS: str = _optional("API_SECRET_KEY_PREVIOUS", "")

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

    # The "Open a Ticket" panel embed, posted into each guild's configured ticket
    # panel channel on startup and by /ticketpanel.
    #
    # Default on. Set TICKET_PANEL_ENABLED=false on any instance that shares a guild
    # with another one — a dev or testing bot next to the live bot. The auto-post is
    # idempotent only against *its own* previous panel: `_find_existing_panel` skips
    # messages this bot user did not write, which is right (it must not adopt a
    # message it cannot edit or attach its view to) and means a second bot cannot see
    # the live panel and posts a duplicate underneath it. The button on that duplicate
    # is served by whichever process posted it, so tickets opened from it land in the
    # test bot's hands.
    #
    # This is a property of the *instance*, not of the guild, which is why it is an
    # env var and not a `guild_config` channel setting: the two bots read the same
    # Firestore config and would read the same answer out of it.
    TICKET_PANEL_ENABLED: bool = _optional("TICKET_PANEL_ENABLED", "true").lower() not in ("false", "0", "no", "off")

    # Multiplayer account API (/api/v1/mp/*). Default **off**: this is a brand new
    # surface for a layer that is not built yet, and a live account service should
    # not carry an unfinished attack surface on the chance somebody finds it. Turn
    # it on deliberately with MULTIPLAYER_ENABLED=true once there is a server to
    # talk to. While off, every /mp/ route answers 404 — not 403 — so the surface
    # is invisible rather than merely closed, the same posture the admin console
    # takes toward a non-admin.
    MULTIPLAYER_ENABLED: bool = _optional("MULTIPLAYER_ENABLED", "false").lower() in ("true", "1", "yes", "on")

    # Mod version gate. When on, the KSP client reports its DLL's SHA256 and the
    # server compares it against the published latest hash (see /admin publishversion
    # and config/mod_version in Firestore); an outdated client is blocked in-game
    # until the player updates. Default on; set KSP_VERSION_CHECK_ENABLED=false in
    # .env to disable the gate (clients are then never told to update). When no
    # version has been published yet, the gate never blocks regardless of this flag.
    KSP_VERSION_CHECK_ENABLED: bool = _optional("KSP_VERSION_CHECK_ENABLED", "true").lower() not in ("false", "0", "no", "off")

    # Cheat disqualification for contract submissions. When on, a submission whose
    # client-side cheat report marks the vessel as moved by HyperEdit / VesselMover /
    # the F12 cheat menu's Set Position-Set Orbit, or flown with F12 cheat toggles
    # enabled, is refused with the reasons shown to the player (see
    # data/cheat_check.py). A cheat tool merely being installed never disqualifies,
    # and older clients that send no report are never rejected. Default on; set
    # KSP_CHEAT_DISQUALIFY_ENABLED=false to accept such submissions (they still
    # face the issuer's / AI review).
    KSP_CHEAT_DISQUALIFY_ENABLED: bool = _optional("KSP_CHEAT_DISQUALIFY_ENABLED", "true").lower() not in ("false", "0", "no", "off")

    # IPs of trusted reverse proxies (comma-separated, e.g. "127.0.0.1"). When a
    # request's direct peer is one of these, the real client IP is read from
    # X-Forwarded-For for rate limiting. Leave empty when clients connect the API
    # directly — the header is attacker-controlled and is ignored unless the peer
    # is a configured proxy.
    # Entries may be single addresses ("127.0.0.1") OR CIDR ranges
    # ("35.191.0.0/16"), because ranges are what the real deployment needs and the
    # exact-string form could not express them. The bot's own peer is Caddy — one
    # stable address — but website traffic arrives through Google front-ends and a
    # Cloud Run egress, which are RANGES. With only exact strings, a list naming Caddy
    # plus one guessed address stops `_client_ip`'s walk on a Google address and returns
    # THAT for every website visitor: the collapsed single bucket the whole trusted-proxy
    # mechanism exists to prevent, now armed rather than merely absent. So the setting
    # was unusable for its main purpose, which is why it has stayed empty — and empty
    # disables eleven per-IP limiters.
    #
    # A malformed entry is DROPPED with a warning rather than raising: a typo in one
    # range must not stop the bot booting, and the failure direction (that hop is not
    # trusted, so the walk stops there) is the safe one.
    _raw_proxies = _optional("API_TRUSTED_PROXIES", "")
    API_TRUSTED_PROXIES: set[str] = {p.strip() for p in _raw_proxies.split(",") if p.strip()}

    @staticmethod
    def _parse_proxy_networks(raw: set[str]) -> list:
        import ipaddress
        nets = []
        for entry in raw:
            try:
                nets.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                print(f"[config] Ignoring unparseable API_TRUSTED_PROXIES entry: {entry!r}")
        return nets

    API_TRUSTED_PROXY_NETS: list = []

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
# Parsed once at import, after the instance exists. Kept alongside the raw strings
# rather than replacing them so the startup warning below can still print what was
# configured, and so an operator reading the value sees what they typed.
cfg.API_TRUSTED_PROXY_NETS = Config._parse_proxy_networks(cfg.API_TRUSTED_PROXIES)

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

# A non-placeholder but short/low-entropy key is still forgeable: the token
# payload is base64-visible, so anyone holding a single token can brute-force a
# weak HMAC key offline and then mint a token for any user. Require a real key.
_MIN_API_SECRET_LEN = 32
if cfg.KSP_API_ENABLED and len(cfg.API_SECRET_KEY.strip()) < _MIN_API_SECRET_LEN:
    raise EnvironmentError(
        f"API_SECRET_KEY is too short ({len(cfg.API_SECRET_KEY.strip())} chars); "
        f"it must be at least {_MIN_API_SECRET_LEN}. It signs KSP session tokens, "
        "and a short key can be brute-forced offline from any one token. Generate "
        "a strong value:\n"
        "    python -c \"import secrets; print(secrets.token_urlsafe(48))\""
    )

# The previous key only exists to widen the VERIFY accept list during rotation.
# A placeholder value would widen it to a publicly known key (token forgery), and
# a copy of the current key adds nothing — blank both out rather than serve them.
if (cfg.API_SECRET_KEY_PREVIOUS.strip() in _DEFAULT_API_SECRETS
        or cfg.API_SECRET_KEY_PREVIOUS == cfg.API_SECRET_KEY):
    cfg.API_SECRET_KEY_PREVIOUS = ""

# ── The security-gate register ───────────────────────────────────────────────
#
# Every flag that weakens a security control in exchange for developer
# convenience is registered HERE, and the startup banner is derived from this
# table rather than from its own hand-written list. That list had drifted: it
# named three gates while five existed, so `KSP_CHEAT_DISQUALIFY_ENABLED=false`
# and `DEBUG_ENDPOINTS_ENABLED=true` ran silently — the failure mode this banner
# exists to prevent, reintroduced by the banner itself being incomplete.
#
# Each entry is (env name, current value, the value that is SAFE). Note that the
# safe value is not always True: `DEBUG_ENDPOINTS_ENABLED` is the one that is
# dangerous when *on*, which is precisely why a plain "which of these are False"
# list could never have covered it.
#
# Adding a gate flag anywhere in this file without adding it here is the bug.
def insecure_gates() -> list[str]:
    """The registered gates that are not in their secure state, for the banner."""
    register = (
        ("KSP_VERSION_CHECK_ENABLED", cfg.KSP_VERSION_CHECK_ENABLED, True),
        ("KSP_DEVICE_BINDING_ENABLED", cfg.KSP_DEVICE_BINDING_ENABLED, True),
        ("KSP_2FA_ENABLED", cfg.KSP_2FA_ENABLED, True),
        ("KSP_CHEAT_DISQUALIFY_ENABLED", cfg.KSP_CHEAT_DISQUALIFY_ENABLED, True),
        ("DEBUG_ENDPOINTS_ENABLED", cfg.DEBUG_ENDPOINTS_ENABLED, False),
        ("MULTIPLAYER_ENABLED", cfg.MULTIPLAYER_ENABLED, False),
    )
    return [f"{name}={str(value).lower()}"
            for name, value, safe in register if value != safe]


# Configure root logger once here so every module inherits it
logging.basicConfig(
    level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# A browser front-end (CORS origins configured) is almost always served through
# a reverse proxy, so if no trusted proxy is set every request's peer is the
# proxy and they all share ONE rate-limit bucket — one user tripping a limit
# then locks everyone out (self-DoS). Warn rather than fail: a same-host proxy
# setup could legitimately omit this. (X-Forwarded-For is still ignored from an
# untrusted peer, so this is never a spoofing risk — only a bucketing one.)
if cfg.KSP_API_ENABLED and cfg.API_CORS_ORIGINS and not cfg.API_TRUSTED_PROXIES:
    logging.getLogger("config").warning(
        "API_CORS_ORIGINS is set but API_TRUSTED_PROXIES is empty. Behind a "
        "reverse proxy this collapses per-IP rate limiting to a single shared "
        "bucket (self-DoS). Set API_TRUSTED_PROXIES to the proxy IP(s).")
