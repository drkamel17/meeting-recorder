"""Persistent application settings stored in data/settings.json."""

import json
import logging
import os
import secrets

from src.config import APP_SETTINGS_PATH
from src.security import decrypt_password, encrypt_password

logger = logging.getLogger("ZoomAutoRecorder")

ENGINE_KAMELREC = "kamelrec"
ENGINE_WINDOWS_GAMEBAR = "windows_gamebar"
VALID_ENGINES = {ENGINE_KAMELREC, ENGINE_WINDOWS_GAMEBAR}
DEFAULT_ENGINE = ENGINE_KAMELREC

ENGINE_LABELS = {
    ENGINE_KAMELREC: "KamelRec (recommandé)",
    ENGINE_WINDOWS_GAMEBAR: "Windows Game Bar",
}


def load_app_settings() -> dict:
    try:
        if APP_SETTINGS_PATH.exists():
            # utf-8-sig : tolère un éventuel BOM (fichier édité à la main).
            data = json.loads(APP_SETTINGS_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                return data
    except json.JSONDecodeError:
        logger.warning("Paramètres illisibles: %s", APP_SETTINGS_PATH)
        # Conserver une copie du fichier abîmé : une sauvegarde ultérieure ne
        # doit pas faire perdre définitivement les réglages existants.
        try:
            backup = APP_SETTINGS_PATH.with_name(APP_SETTINGS_PATH.name + ".broken")
            backup.write_bytes(APP_SETTINGS_PATH.read_bytes())
            logger.warning("Copie de secours des paramètres: %s", backup)
        except OSError:
            pass
    except OSError as e:
        logger.warning("Lecture des paramètres impossible: %s", e)
    return {}


def save_app_settings(data: dict) -> bool:
    try:
        APP_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        APP_SETTINGS_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except OSError as e:
        logger.warning("Écriture des paramètres impossible: %s", e)
        return False


def get_recording_engine() -> str:
    settings = load_app_settings()
    engine = settings.get("recording_engine", DEFAULT_ENGINE)
    if engine not in VALID_ENGINES:
        logger.warning("Enregistreur inconnu (%s), utilisation de KamelRec.", engine)
        return DEFAULT_ENGINE
    return engine


def set_recording_engine(engine: str) -> bool:
    if engine not in VALID_ENGINES:
        return False
    settings = load_app_settings()
    settings["recording_engine"] = engine
    return save_app_settings(settings)


def get_close_popups_during_recording() -> bool:
    """Fermer automatiquement les fenêtres qui surgissent pendant l'enregistrement."""
    settings = load_app_settings()
    value = settings.get("close_popups_during_recording", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "non", "no", "off")
    return bool(value)


def set_close_popups_during_recording(enabled: bool) -> bool:
    settings = load_app_settings()
    settings["close_popups_during_recording"] = bool(enabled)
    return save_app_settings(settings)


# ═══════════════════════════════════════════════════════════════
# CONTRÔLE À DISTANCE (page web + e-mail Gmail)
# ═══════════════════════════════════════════════════════════════
DEFAULT_REMOTE_PORT = 8080
DEFAULT_REMOTE_EMAIL_KEYWORD = "record"
DEFAULT_REMOTE_EMAIL_POLL = 60

# Alphabet sans caractères ambigus (pas de i, l, o, 0, 1) : un jeton lu à
# l'écran ne peut pas être confondu (I/l, O/0).
REMOTE_TOKEN_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
REMOTE_TOKEN_LENGTH = 24


def generate_remote_token(length: int = REMOTE_TOKEN_LENGTH) -> str:
    return "".join(secrets.choice(REMOTE_TOKEN_ALPHABET) for _ in range(length))


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "non", "no", "off", "")
    return bool(value)


def _as_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def get_remote_web_enabled() -> bool:
    return _as_bool(load_app_settings().get("remote_web_enabled"), False)


def set_remote_web_enabled(enabled: bool) -> bool:
    settings = load_app_settings()
    settings["remote_web_enabled"] = bool(enabled)
    return save_app_settings(settings)


def get_remote_web_port() -> int:
    return _as_int(load_app_settings().get("remote_web_port"), DEFAULT_REMOTE_PORT, 1024, 65535)


def set_remote_web_port(port: int) -> bool:
    settings = load_app_settings()
    settings["remote_web_port"] = _as_int(port, DEFAULT_REMOTE_PORT, 1024, 65535)
    return save_app_settings(settings)


def get_remote_web_token(create: bool = True) -> str:
    """Return the access token, generating and saving one on first use."""
    settings = load_app_settings()
    token = str(settings.get("remote_web_token") or "").strip()
    if token:
        return token
    if not create:
        return ""
    token = generate_remote_token()
    settings["remote_web_token"] = token
    save_app_settings(settings)
    return token


def set_remote_web_token(token: str) -> bool:
    settings = load_app_settings()
    settings["remote_web_token"] = str(token or "").strip()
    return save_app_settings(settings)


def regenerate_remote_web_token() -> str:
    token = generate_remote_token()
    set_remote_web_token(token)
    return token


def get_remote_email_enabled() -> bool:
    return _as_bool(load_app_settings().get("remote_email_enabled"), False)


def set_remote_email_enabled(enabled: bool) -> bool:
    settings = load_app_settings()
    settings["remote_email_enabled"] = bool(enabled)
    return save_app_settings(settings)


def get_remote_email_address() -> str:
    return str(load_app_settings().get("remote_email_address") or "").strip()


def set_remote_email_address(address: str) -> bool:
    settings = load_app_settings()
    settings["remote_email_address"] = str(address or "").strip()
    return save_app_settings(settings)


def get_remote_email_password() -> str:
    """Return the decrypted Gmail app password (empty when not set)."""
    raw = str(load_app_settings().get("remote_email_password") or "")
    if not raw:
        return ""
    try:
        return decrypt_password(raw)
    except Exception:
        logger.warning("Mot de passe e-mail illisible.")
        return ""


def set_remote_email_password(password: str) -> bool:
    settings = load_app_settings()
    password = str(password or "").strip()
    settings["remote_email_password"] = encrypt_password(password) if password else ""
    return save_app_settings(settings)


def get_remote_email_keyword() -> str:
    value = str(load_app_settings().get("remote_email_keyword") or "").strip()
    return value or DEFAULT_REMOTE_EMAIL_KEYWORD


def set_remote_email_keyword(keyword: str) -> bool:
    settings = load_app_settings()
    settings["remote_email_keyword"] = str(keyword or "").strip() or DEFAULT_REMOTE_EMAIL_KEYWORD
    return save_app_settings(settings)


def get_remote_email_senders() -> list[str]:
    raw = load_app_settings().get("remote_email_senders") or ""
    if isinstance(raw, list):
        items = raw
    else:
        items = str(raw).replace(";", ",").split(",")
    return [str(item).strip().lower() for item in items if str(item).strip()]


def set_remote_email_senders(senders) -> bool:
    if isinstance(senders, (list, tuple, set)):
        value = ", ".join(str(item).strip() for item in senders if str(item).strip())
    else:
        value = str(senders or "").strip()
    settings = load_app_settings()
    settings["remote_email_senders"] = value
    return save_app_settings(settings)


def get_remote_email_poll_seconds() -> int:
    return _as_int(
        load_app_settings().get("remote_email_poll_seconds"),
        DEFAULT_REMOTE_EMAIL_POLL,
        15,
        3600,
    )


def set_remote_email_poll_seconds(seconds: int) -> bool:
    settings = load_app_settings()
    settings["remote_email_poll_seconds"] = _as_int(
        seconds, DEFAULT_REMOTE_EMAIL_POLL, 15, 3600
    )
    return save_app_settings(settings)


def get_remote_autostart() -> bool:
    return _as_bool(load_app_settings().get("remote_autostart"), False)


def set_remote_autostart(enabled: bool) -> bool:
    settings = load_app_settings()
    settings["remote_autostart"] = bool(enabled)
    return save_app_settings(settings)


def get_remote_autostart_default_done() -> bool:
    """True une fois la proposition « démarrer avec Windows » appliquée.

    Migration silencieuse : à la première utilisation de la page web, le
    démarrage automatique est activé pour que le lien reste joignable même
    après une fermeture ou un redémarrage du PC. Si l'utilisateur décoche
    ensuite la case, son choix est respecté.
    """
    return _as_bool(load_app_settings().get("remote_autostart_default_done"), False)


def set_remote_autostart_default_done(done: bool = True) -> bool:
    settings = load_app_settings()
    settings["remote_autostart_default_done"] = bool(done)
    return save_app_settings(settings)


def get_remote_config() -> dict:
    """Complete remote-control configuration (password already decrypted)."""
    return {
        "web_enabled": get_remote_web_enabled(),
        "web_port": get_remote_web_port(),
        "web_token": get_remote_web_token(create=True),
        "email_enabled": get_remote_email_enabled(),
        "email_address": get_remote_email_address(),
        "email_password": get_remote_email_password(),
        "email_keyword": get_remote_email_keyword(),
        "email_senders": get_remote_email_senders(),
        "email_poll_seconds": get_remote_email_poll_seconds(),
        "autostart": get_remote_autostart(),
    }
