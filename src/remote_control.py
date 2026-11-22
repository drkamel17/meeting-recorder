"""Contrôle à distance de Zoom Auto Recorder.

Deux canaux sont disponibles, utilisables ensemble :

* une **page web** protégée par un jeton (accessible depuis un téléphone via
  Tailscale, un tunnel Cloudflare, ou le réseau local) ;
* une **boîte Gmail** surveillée en IMAP : un e-mail contenant un lien Zoom /
  Google Meet et un mot-clé déclenche l'enregistrement.

Le module ne dépend pas de Qt : il expose une file d'actions que l'application
consomme depuis son thread graphique (``RemoteControlService.poll``).
"""

from __future__ import annotations

import hmac
import html
import imaplib
import json
import logging
import queue
import re
import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

from src.config import DATA_DIR

logger = logging.getLogger("ZoomAutoRecorder")

EMAIL_HOST = "imap.gmail.com"
EMAIL_PORT = 993
EMAIL_STATE_PATH = DATA_DIR / "remote_email_state.json"


# ═══════════════════════════════════════════════════════════════
# MODÈLE D'ORDRE
# ═══════════════════════════════════════════════════════════════
class RemoteOrderError(ValueError):
    """Ordre distant invalide (lien/ID manquant, date incorrecte…)."""


@dataclass
class RemoteOrder:
    platform: str = "zoom"          # "zoom" ou "google_meet"
    meeting_id: str = ""            # ID Zoom ou code Meet
    passcode: str = ""
    display_name: str = ""
    title: str = ""
    duration_minutes: int = 60
    start_at: Optional[datetime] = None
    source: str = "web"             # "web" ou "email"
    sender: str = ""
    subject: str = ""
    raw: str = field(default="", repr=False)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "meeting_id": self.meeting_id,
            "passcode": self.passcode,
            "display_name": self.display_name,
            "title": self.title,
            "duration_minutes": self.duration_minutes,
            "start_at": self.start_at.isoformat(timespec="minutes") if self.start_at else None,
            "source": self.source,
            "sender": self.sender,
            "subject": self.subject,
        }


# ═══════════════════════════════════════════════════════════════
# ANALYSE DU TEXTE (lien, ID, code, nom, durée)
# ═══════════════════════════════════════════════════════════════
_ZOOM_URL_RE = re.compile(
    r"https?://(?:[\w.-]+\.)?zoom\.us/(?:j|w|my)/(\d{9,12})(?:\?([^\s<>\"']*))?",
    re.IGNORECASE,
)
_ZOOM_MTG_RE = re.compile(
    r"zoommtg://[^\s<>\"']*?confno=(\d{9,12})[^\s<>\"']*",
    re.IGNORECASE,
)
_MEET_URL_RE = re.compile(
    r"https?://meet\.google\.com/(?:meet/)?([a-z0-9][a-z0-9-]{4,})",
    re.IGNORECASE,
)
_PWD_RE = re.compile(r"[?&]pwd=([A-Za-z0-9._~-]+)", re.IGNORECASE)
_PLAIN_ZOOM_ID_RE = re.compile(r"(?<!\d)((?:\d[\s-]?){8,13}\d)(?!\d)")

# Étiquettes multilingues (français, anglais, arabe).
_FIELD_LABELS = {
    "meeting_id": [
        "meeting id", "meeting number", "meeting no", "conf no", "confno",
        "numéro de réunion", "numero de reunion", "numéro", "numero",
        "identifiant", "id", "رقم الاجتماع", "معرف الاجتماع", "المعرف",
    ],
    "passcode": [
        "passcode", "pass code", "mot de passe", "motdepasse", "mdp",
        "pwd", "code", "كلمة المرور", "كلمة السر", "الرمز", "رمز", "كود",
    ],
    "display_name": [
        "display name", "displayname", "nom affiché", "nom affiche",
        "nom", "name", "الاسم", "اسم",
    ],
    "title": [
        "meeting name", "meeting title", "meeting", "réunion", "reunion",
        "titre", "title", "sujet", "subject",
        "اسم الاجتماع", "عنوان الاجتماع", "الاجتماع", "عنوان",
    ],
    "duration": [
        "duration", "durée", "duree", "minutes", "mins", "min",
        "المدة", "مدة",
    ],
    "start": [
        "start time", "start", "début", "debut", "date et heure", "date",
        "heure", "وقت البدء", "البداية", "التاريخ", "الوقت",
    ],
}

_LABEL_TO_FIELD: dict[str, str] = {}
for _field, _labels in _FIELD_LABELS.items():
    for _label in _labels:
        _LABEL_TO_FIELD[_label.lower()] = _field

_ALL_LABELS = sorted(_LABEL_TO_FIELD, key=len, reverse=True)
_LABEL_ALTERNATION = "|".join(re.escape(label) for label in _ALL_LABELS)
_LABEL_RE = re.compile(
    r"^\s*(" + _LABEL_ALTERNATION + r")\s*[:=]\s*(.+?)\s*$",
    re.IGNORECASE,
)
# Étiquettes en milieu de ligne : « ID 123 code: 456 nom: Karim ».
_INLINE_LABEL_RE = re.compile(
    r"(?:^|(?<=\s))(" + _LABEL_ALTERNATION + r")\s*[:=]\s*"
    r"(.*?)(?=\s+(?:" + _LABEL_ALTERNATION + r")\s*[:=]|\n|$)",
    re.IGNORECASE | re.MULTILINE,
)


def _first(params: dict, key: str, default: str = "") -> str:
    values = params.get(key) or []
    if isinstance(values, str):
        return values
    return str(values[0]) if values else default


def _parse_multipart(raw: bytes, content_type: str) -> dict:
    """Extrait les champs d'un corps ``multipart/form-data``.

    Les navigateurs envoient ``new FormData(form)`` en multipart : sans ce
    décodage, le jeton et les champs du formulaire seraient ignorés (403).
    """
    match = re.search(r'boundary="?([^";]+)"?', content_type, re.IGNORECASE)
    if not match:
        return {}
    boundary = ("--" + match.group(1).strip()).encode("latin-1", "ignore")
    fields: dict = {}
    for chunk in raw.split(boundary):
        if not chunk:
            continue
        chunk = chunk.lstrip(b"\r\n")
        if not chunk or chunk.startswith(b"--"):
            continue
        head, sep, body = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        if body.endswith(b"\r\n"):
            body = body[:-2]
        headers = head.decode("latin-1", "ignore")
        name_match = re.search(r'name="([^"]*)"', headers, re.IGNORECASE)
        if not name_match:
            continue
        try:
            fields[name_match.group(1)] = body.decode("utf-8")
        except UnicodeDecodeError:
            fields[name_match.group(1)] = body.decode("latin-1", "ignore")
    return fields


def _parse_start(value: str) -> Optional[datetime]:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%d/%m/%y %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_duration(value: str, default: int = 60) -> int:
    match = re.search(r"\d+", str(value or ""))
    if not match:
        return default
    try:
        return max(1, min(1440, int(match.group(0))))
    except ValueError:
        return default


def _extract_target(text: str) -> tuple[str, str, str]:
    """Return (platform, meeting_id, url_passcode) from a text or bare ID."""
    text = text or ""

    zoom_match = _ZOOM_URL_RE.search(text)
    zoom_mtg_match = _ZOOM_MTG_RE.search(text)
    meet_match = _MEET_URL_RE.search(text)

    candidates = []
    if zoom_match:
        candidates.append((zoom_match.start(), "zoom", zoom_match.group(1)))
    if zoom_mtg_match:
        candidates.append((zoom_mtg_match.start(), "zoom", zoom_mtg_match.group(1)))
    if meet_match:
        candidates.append((meet_match.start(), "google_meet", meet_match.group(1)))
    if candidates:
        candidates.sort(key=lambda item: item[0])
        _, platform, meeting_id = candidates[0]
    else:
        platform, meeting_id = "zoom", ""

    passcode = ""
    pwd_match = _PWD_RE.search(text)
    if pwd_match:
        passcode = pwd_match.group(1)

    if not meeting_id:
        plain = _PLAIN_ZOOM_ID_RE.search(text)
        if plain:
            digits = re.sub(r"\D", "", plain.group(1))
            # Un simple nombre à 9-12 chiffres n'est accepté que s'il est seul
            # ou accompagné d'un contexte Zoom/ID (évite les numéros de
            # téléphone dans une signature d'e-mail).
            only_number = bool(re.fullmatch(r"\d{9,12}", re.sub(r"[\s\-().+]", "", text)))
            context = bool(
                re.search(
                    r"zoom|\b(?:id|meeting|conf|webinar|join|r[ée]union|rejoindre)\b"
                    r"|اجتماع|الاجتماع|انضم",
                    text,
                    re.IGNORECASE,
                )
            )
            if 9 <= len(digits) <= 12 and (only_number or context):
                meeting_id = digits
                platform = "zoom"

    return platform, meeting_id, passcode


def parse_remote_order(text: str, source: str = "web", default_title: str = "") -> RemoteOrder:
    """Analyse un texte libre (corps d'e-mail ou formulaire) en ordre distant.

    Formats acceptés : lien Zoom / Google Meet, ID Zoom seul, et lignes
    ``clé: valeur`` (français, anglais ou arabe) pour le code, le nom, le
    titre, la durée et l'heure de début.
    """
    text = text or ""
    platform, meeting_id, url_passcode = _extract_target(text)

    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _LABEL_RE.match(line)
        if not match:
            continue
        label = match.group(1).lower()
        field_name = _LABEL_TO_FIELD.get(label)
        if field_name and field_name not in values:
            values[field_name] = match.group(2).strip()

    # Étiquettes au milieu d'une ligne (ex. « 123 456 7890 code: 778899 »).
    for match in _INLINE_LABEL_RE.finditer(text):
        label = match.group(1).lower()
        field_name = _LABEL_TO_FIELD.get(label)
        if field_name and field_name not in values:
            values[field_name] = match.group(2).strip()

    if not meeting_id and values.get("meeting_id"):
        raw_id = values["meeting_id"]
        meet = _MEET_URL_RE.search(raw_id)
        if meet:
            platform, meeting_id = "google_meet", meet.group(1)
        else:
            digits = re.sub(r"\D", "", raw_id)
            if 9 <= len(digits) <= 12:
                platform, meeting_id = "zoom", digits
            elif raw_id.strip():
                platform, meeting_id = "google_meet", raw_id.strip()

    if not meeting_id:
        raise RemoteOrderError(
            "Aucun lien ou identifiant de réunion trouvé. "
            "Envoyez un lien Zoom / Google Meet ou un ID à 9-12 chiffres."
        )

    if platform == "google_meet":
        meeting_id = meeting_id.strip()

    passcode = values.get("passcode") or url_passcode
    display_name = values.get("display_name", "").strip()
    title = values.get("title", "").strip() or (default_title or "").strip()
    if not title:
        title = display_name
    if not display_name:
        display_name = title

    duration = _parse_duration(values.get("duration", ""), 60)
    start_at = _parse_start(values.get("start", ""))
    if start_at and start_at <= datetime.now():
        start_at = None

    return RemoteOrder(
        platform=platform,
        meeting_id=meeting_id,
        passcode=passcode.strip(),
        display_name=display_name.strip(),
        title=title.strip(),
        duration_minutes=duration,
        start_at=start_at,
        source=source,
        raw=text,
    )


def build_order_from_form(
    target: str,
    passcode: str = "",
    display_name: str = "",
    title: str = "",
    duration: str = "",
    start_at: str = "",
) -> RemoteOrder:
    """Construit un ordre à partir des champs de la page web."""
    order = parse_remote_order(target, source="web")
    if passcode.strip():
        order.passcode = passcode.strip()
    if display_name.strip():
        order.display_name = display_name.strip()
    if title.strip():
        order.title = title.strip()
    if duration.strip():
        order.duration_minutes = _parse_duration(duration, order.duration_minutes)
    if start_at.strip():
        parsed = _parse_start(start_at)
        if parsed and parsed > datetime.now():
            order.start_at = parsed
        elif parsed:
            order.start_at = None
    if not order.title:
        order.title = order.display_name
    if not order.display_name:
        order.display_name = order.title
    return order


# ═══════════════════════════════════════════════════════════════
# PAGE WEB
# ═══════════════════════════════════════════════════════════════
_PAGE_TEMPLATE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zoom Auto Recorder — Contrôle à distance</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, -apple-system, "Segoe UI", Arial, sans-serif;
         background:#eef2f7; color:#1f2933; }
  .wrap { max-width: 620px; margin: 0 auto; padding: 18px 14px 40px; }
  h1 { font-size: 21px; margin: 6px 0 2px; }
  h2 { font-size: 15px; margin: 0 0 10px; }
  .sub { color:#5f6b7a; font-size: 13px; margin: 0 0 16px; }
  .card { background:#fff; border:1px solid #d8e0ea; border-radius:10px;
          padding:16px; margin-top:14px; box-shadow:0 1px 2px rgba(0,0,0,.04); }
  form.card { margin-top:0; }
  label { display:block; font-size:12px; font-weight:600; color:#3e4c59;
          margin:10px 0 4px; }
  .ar { font-weight:400; color:#7b8794; }
  input { width:100%; padding:10px 11px; font-size:15px; border:1px solid #cbd5e0;
          border-radius:8px; background:#fff; }
  input:focus { outline:2px solid #2b6cb0; border-color:#2b6cb0; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:0 12px; }
  button { width:100%; padding:12px; font-size:15px; font-weight:700; border:0;
           border-radius:8px; color:#fff; margin-top:14px; cursor:pointer; }
  .primary { background:#2f855a; }
  .danger { background:#c53030; margin-top:10px; }
  .msg { padding:11px 13px; border-radius:8px; font-size:13px; margin:0 0 14px; }
  .ok { background:#e6fffa; border:1px solid #81e6d9; color:#1d4044; }
  .err { background:#fff5f5; border:1px solid #feb2b2; color:#742a2a; }
  #status { font-size:14px; line-height:1.6; }
  .rec { display:flex; justify-content:space-between; gap:10px; padding:8px 0;
         border-bottom:1px solid #edf2f7; font-size:13px; }
  .rec:last-child { border-bottom:0; }
  .rec a { color:#2b6cb0; text-decoration:none; font-weight:600; }
  .rec span { color:#7b8794; white-space:nowrap; }
  .hint { color:#718096; font-size:11px; line-height:1.5; margin-top:18px; }
  @media (max-width: 460px) { .grid { grid-template-columns:1fr; } }
</style>
</head>
<body>
<div class="wrap">
  <h1>🎥 Zoom Auto Recorder</h1>
  <p class="sub">Contrôle à distance — التحكم عن بعد</p>
  __MESSAGE__
  <form class="card" id="record-form" method="post" action="/record">
    __TOKEN_BLOCK__
    <div id="formmsg"></div>
    <label>Lien Zoom / Google Meet ou ID <span class="ar">(رابط أو رقم الاجتماع)</span></label>
    <input name="target" required autofocus
           placeholder="https://zoom.us/j/1234567890?pwd=... ou 123 456 7890">
    <div class="grid">
      <div>
        <label>Code secret <span class="ar">(كلمة المرور)</span></label>
        <input name="passcode" placeholder="123456" autocomplete="off">
      </div>
      <div>
        <label>Nom affiché <span class="ar">(الاسم)</span></label>
        <input name="display_name" placeholder="Kamel" autocomplete="off">
      </div>
    </div>
    <div class="grid">
      <div>
        <label>Nom de la réunion <span class="ar">(اسم الاجتماع)</span></label>
        <input name="title" placeholder="Réunion">
      </div>
      <div>
        <label>Durée (minutes) <span class="ar">(المدة)</span></label>
        <input name="duration" type="number" min="1" max="1440" value="60">
      </div>
    </div>
    <label>Début planifié (optionnel) <span class="ar">(وقت البدء)</span></label>
    <input name="start_at" type="datetime-local">
    <button class="primary" type="submit" id="record-btn">🎥 Enregistrer maintenant</button>
  </form>

  <div class="card">
    <h2>État <span class="ar">(الحالة)</span></h2>
    <div id="status">Chargement…</div>
    <button id="stop" class="danger" type="button">⏹ Arrêter l'enregistrement</button>
  </div>

  <div class="card">
    <h2>Enregistrements récents <span class="ar">(التسجيلات)</span></h2>
    <div id="recordings">Chargement…</div>
  </div>

  <p class="hint">Accès protégé par un jeton. Ne partagez pas ce lien.<br>
  Le PC doit rester allumé, connecté à Internet et la session Windows ouverte.</p>
</div>
<script>
  let TOKEN = __JS_TOKEN__;
  const statusEl = document.getElementById("status");
  const recEl = document.getElementById("recordings");
  const form = document.getElementById("record-form");
  const formMsg = document.getElementById("formmsg");
  const recordBtn = document.getElementById("record-btn");

  function q() {
    return TOKEN ? ("?token=" + encodeURIComponent(TOKEN)) : "";
  }
  function fmt(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
  }
  function esc(text) {
    const d = document.createElement("div");
    d.textContent = text == null ? "" : String(text);
    return d.innerHTML;
  }
  function showFormMsg(kind, text) {
    if (!formMsg) return;
    formMsg.innerHTML = '<div class="msg ' + kind + '">' + esc(text) + "</div>";
  }

  // Le jeton saisi une seule fois est mémorisé dans ce navigateur : plus besoin
  // de le retaper ni de le copier depuis le PC.
  (function restoreToken() {
    if (!form) return;
    const input = form.querySelector('input[name="token"]');
    if (!input) return;
    let saved = "";
    try { saved = localStorage.getItem("zar_token") || ""; } catch (e) {}
    if (input.type !== "hidden") {
      if (saved && !input.value) input.value = saved;
      if (!TOKEN && input.value) TOKEN = input.value.trim();
    }
  })();

  async function refresh() {
    if (!statusEl || !recEl) return;
    if (!TOKEN) {
      statusEl.textContent = "Entrez le jeton d'accès pour voir l'état.";
      recEl.textContent = "Jeton requis.";
      return;
    }
    try {
      const r = await fetch("/api/status" + q(), {cache: "no-store"});
      const s = await r.json();
      if (!r.ok) throw new Error(s.error || "erreur");
      let text = "<b>" + esc(s.state || "—") + "</b>";
      if (s.meeting) text += "<br>Réunion : " + esc(s.meeting);
      text += "<br>Enregistrement : " + (s.recording ? (s.paused ? "en pause" : "en cours") : "non");
      if (s.recording) text += "<br>Durée : " + fmt(s.elapsed);
      if (s.last_error) text += "<br><span style='color:#c53030'>" + esc(s.last_error) + "</span>";
      statusEl.innerHTML = text;
    } catch (e) {
      statusEl.textContent = "Serveur injoignable ou jeton invalide.";
    }
    try {
      const r = await fetch("/api/recordings" + q(), {cache: "no-store"});
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "erreur");
      const items = (j.recordings || []).slice(0, 10);
      recEl.innerHTML = items.length ? items.map(function (x) {
        const label = esc(x.meeting_name || "Enregistrement");
        const date = esc((x.start_time || "").replace("T", " "));
        const href = "/download/" + encodeURIComponent(x.id) + q();
        return '<div class="rec"><a href="' + href + '">' + label + "</a><span>" + date + "</span></div>";
      }).join("") : "Aucun enregistrement.";
    } catch (e) {}
  }

  // Envoi sans rechargement : les champs saisis restent en place en cas d'erreur.
  if (form) form.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    const fd = new FormData(form);
    const data = new URLSearchParams();
    for (const pair of fd.entries()) data.append(pair[0], pair[1]);
    const typed = String(data.get("token") || "").trim();
    if (!typed) {
      showFormMsg("err", "Jeton d'accès requis.");
      return;
    }
    if (recordBtn) { recordBtn.disabled = true; recordBtn.textContent = "Envoi…"; }
    try {
      const r = await fetch("/api/record", {
        method: "POST", body: data.toString(),
        headers: {
          "Accept": "application/json",
          "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"
        }
      });
      let j = {};
      try { j = await r.json(); } catch (e) {}
      if (!r.ok) throw new Error(j.error || ("Erreur " + r.status));
      TOKEN = typed;
      try { localStorage.setItem("zar_token", typed); } catch (e) {}
      const o = j.order || {};
      let msg = "Ordre envoyé : « " + (o.title || o.meeting_id || "") + " ».";
      if (o.start_at) {
        msg += " Démarrage planifié.";
      } else {
        msg += " L'application ouvre la réunion et démarre l'enregistrement.";
      }
      showFormMsg("ok", msg);
      const target = form.querySelector('input[name="target"]');
      if (target) target.value = "";
      refresh();
    } catch (e) {
      showFormMsg("err", e.message || "Échec de l'envoi.");
    } finally {
      if (recordBtn) { recordBtn.disabled = false; recordBtn.textContent = "🎥 Enregistrer maintenant"; }
    }
  });

  const stopBtn = document.getElementById("stop");
  if (stopBtn) stopBtn.addEventListener("click", async function () {
    if (!TOKEN) {
      showFormMsg("err", "Entrez d'abord le jeton d'accès.");
      return;
    }
    if (!confirm("Arrêter l'enregistrement en cours ?")) return;
    try {
      const r = await fetch("/api/stop" + q(), {method: "POST"});
      const j = await r.json();
      if (!r.ok) alert(j.error || "Erreur");
    } catch (e) { alert("Serveur injoignable."); }
    refresh();
  });
  refresh();
  setInterval(refresh, 5000);
</script>
</body>
</html>
"""


def _normalize_token(value: str) -> str:
    """Normalise un jeton pour tolérer les confusions visuelles.

    Sur un clavier/écran, ``I`` (i majuscule), ``l`` (L minuscule) et ``1``
    sont faciles à confondre, de même que ``O`` et ``0``. Les jetons sont donc
    comparés sans tenir compte de la casse et en ramenant ces glyphes à une
    forme unique. L'entropie reste très largement suffisante (24 caractères).
    """
    text = str(value or "").strip().lower()
    return text.replace("i", "l").replace("1", "l").replace("0", "o")


def _token_matches(expected: str, given: str) -> bool:
    if not expected:
        return False
    return hmac.compare_digest(_normalize_token(given), _normalize_token(expected))


def _is_our_server(port: int, timeout: float = 1.5) -> bool:
    """Vrai si le port est déjà servi par une autre instance de l'application."""
    try:
        import urllib.request

        url = f"http://127.0.0.1:{int(port)}/health"
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
        return isinstance(payload, dict) and payload.get("app") == "ZoomAutoRecorder"
    except Exception:
        return False


def render_remote_page(
    service: "RemoteControlService",
    params: dict,
    message: str = "",
    error: str = "",
) -> str:
    token = _first(params, "token") or _first(params, "access_token")
    effective_token = service.current_token()
    authorized = _token_matches(effective_token, token)

    if authorized:
        token_block = (
            '<input type="hidden" name="token" value="'
            + html.escape(effective_token, quote=True)
            + '">'
        )
        js_token = json.dumps(effective_token)
    else:
        # Conserver le jeton saisi (ou celui du lien) pour ne pas le retaper.
        typed = html.escape(str(token or ""), quote=True)
        token_block = (
            '<label>Jeton d\'accès <span class="ar">(رمز الوصول)</span></label>'
            # Champ visible (pas de type=password) : les gestionnaires de mots de
            # passe du navigateur ne le remplissent pas avec une valeur parasite,
            # et on voit exactement ce qui est saisi.
            '<input name="token" type="text" required autocomplete="off" '
            'autocapitalize="none" autocorrect="off" spellcheck="false" '
            'style="font-family:Consolas,\'Courier New\',monospace" '
            'value="' + typed + '" '
            'placeholder="Jeton affiché dans ⚙️ Paramètres">'
        )
        js_token = "null"
        if token and not error:
            error = (
                "Jeton d'accès invalide. Vérifiez le lien (le jeton exact est affiché "
                "dans ⚙️ Paramètres, bouton 📋 Copier)."
            )

    banner = ""
    if error:
        banner += '<div class="msg err">' + html.escape(error) + "</div>"
    if message:
        banner += '<div class="msg ok">' + html.escape(message) + "</div>"

    return (
        _PAGE_TEMPLATE.replace("__MESSAGE__", banner)
        .replace("__TOKEN_BLOCK__", token_block)
        .replace("__JS_TOKEN__", js_token)
    )


# ═══════════════════════════════════════════════════════════════
# SERVEUR HTTP
# ═══════════════════════════════════════════════════════════════
class _RemoteHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, service: "RemoteControlService"):
        self.service = service
        super().__init__(address, handler)


class _RemoteRequestHandler(BaseHTTPRequestHandler):
    server_version = "ZoomAutoRecorder"
    protocol_version = "HTTP/1.1"

    @property
    def service(self) -> "RemoteControlService":
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # noqa: A003
        logger.debug("Page web de contrôle: " + fmt, *args)

    # ── helpers ───────────────────────────────────────────────
    def _send(self, code: int, body, content_type: str = "text/html; charset=utf-8",
              headers: Optional[dict] = None) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def _authorized(self, params: dict) -> bool:
        given = (_first(params, "token")
                 or self.headers.get("X-Remote-Token", "")
                 or self.headers.get("Authorization", "").replace("Bearer ", ""))
        return _token_matches(self.service.current_token(), given)

    def _send_file(self, file_path) -> None:
        if not file_path:
            self._send(404, "Enregistrement introuvable", "text/plain; charset=utf-8")
            return
        try:
            path = Path(str(file_path))
        except (TypeError, ValueError):
            self._send(404, "Enregistrement introuvable", "text/plain; charset=utf-8")
            return
        if not path.is_file():
            self._send(404, "Enregistrement introuvable", "text/plain; charset=utf-8")
            return
        try:
            size = path.stat().st_size
        except OSError:
            self._send(404, "Enregistrement introuvable", "text/plain; charset=utf-8")
            return
        filename = path.name.replace('"', "")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ── GET ───────────────────────────────────────────────────
    def do_GET(self):  # noqa: N802
        parsed = urlsplit(self.path)
        params = parse_qs(parsed.query, keep_blank_values=True)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/":
                self._send(200, render_remote_page(self.service, params))
            elif path == "/health":
                self._json(200, {"ok": True, "app": "ZoomAutoRecorder"})
            elif path == "/api/status":
                if not self._authorized(params):
                    self._json(403, {"error": "Jeton invalide"})
                    return
                self._json(200, self.service.get_status())
            elif path == "/api/recordings":
                if not self._authorized(params):
                    self._json(403, {"error": "Jeton invalide"})
                    return
                self._json(200, {"recordings": self.service.get_recordings()})
            elif path.startswith("/download/"):
                if not self._authorized(params):
                    self._send(403, "Jeton invalide", "text/plain; charset=utf-8")
                    return
                self._send_file(self.service.get_download_path(path.rsplit("/", 1)[-1]))
            else:
                self._send(404, "Page introuvable", "text/plain; charset=utf-8")
        except Exception as exc:  # pragma: no cover - défensif
            logger.warning("Page web de contrôle: %s", exc)
            try:
                self._send(500, "Erreur interne", "text/plain; charset=utf-8")
            except Exception:
                pass

    # ── POST ──────────────────────────────────────────────────
    def do_POST(self):  # noqa: N802
        parsed = urlsplit(self.path)
        params = parse_qs(parsed.query, keep_blank_values=True)
        path = parsed.path.rstrip("/") or "/"
        content_type = self.headers.get("Content-Type", "")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(min(length, 1024 * 1024)) if length > 0 else b""
        is_api = path.startswith("/api/") or "application/json" in content_type

        data: dict = {}
        if "application/json" in content_type:
            try:
                parsed_json = json.loads(raw.decode("utf-8"))
                if isinstance(parsed_json, dict):
                    data = parsed_json
            except (UnicodeDecodeError, json.JSONDecodeError):
                data = {}
        elif "multipart/form-data" in content_type:
            data = _parse_multipart(raw, content_type)
        else:
            try:
                form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            except UnicodeDecodeError:
                form = {}
            data = {key: _first(form, key) for key in form}

        given_token = (str(data.get("token") or "")
                       or _first(params, "token")
                       or self.headers.get("X-Remote-Token", "")
                       or self.headers.get("Authorization", "").replace("Bearer ", ""))
        if not _token_matches(self.service.current_token(), given_token):
            if is_api:
                self._json(403, {"error": "Jeton invalide"})
            else:
                self._send(403, render_remote_page(
                    self.service, {"token": [given_token]},
                    error="Jeton d'accès invalide. Rechargez la page avec le bon lien.",
                ))
            return

        try:
            if path in ("/record", "/api/record"):
                order = build_order_from_form(
                    target=str(data.get("target") or data.get("meeting") or ""),
                    passcode=str(data.get("passcode") or ""),
                    display_name=str(data.get("display_name") or ""),
                    title=str(data.get("title") or ""),
                    duration=str(data.get("duration") or ""),
                    start_at=str(data.get("start_at") or ""),
                )
                self.service.enqueue_order(order)
                if order.start_at:
                    message = (
                        f"Ordre enregistré : « {order.title or order.meeting_id} » "
                        f"sera lancé le {order.start_at:%d/%m/%Y à %H:%M}."
                    )
                else:
                    message = (
                        f"Ordre reçu : « {order.title or order.meeting_id} ». "
                        "L'application ouvre la réunion et démarre l'enregistrement."
                    )
                logger.info("🌐 Ordre reçu par la page web : %s (%s)", order.meeting_id, order.platform)
                if is_api:
                    self._json(200, {"ok": True, "order": order.to_dict()})
                else:
                    self._send(200, render_remote_page(
                        self.service, {"token": [given_token]}, message=message,
                    ))
            elif path in ("/stop", "/api/stop"):
                self.service.enqueue_stop()
                if is_api:
                    self._json(200, {"ok": True, "message": "Arrêt demandé."})
                else:
                    self._send(200, render_remote_page(
                        self.service, {"token": [given_token]},
                        message="Arrêt de l'enregistrement demandé.",
                    ))
            else:
                if is_api:
                    self._json(404, {"error": "Action inconnue"})
                else:
                    self._send(404, "Action inconnue", "text/plain; charset=utf-8")
        except RemoteOrderError as exc:
            if is_api:
                self._json(400, {"error": str(exc)})
            else:
                self._send(400, render_remote_page(
                    self.service, {"token": [given_token]}, error=str(exc),
                ))
        except Exception as exc:  # pragma: no cover - défensif
            logger.warning("Page web de contrôle (POST): %s", exc)
            if is_api:
                self._json(500, {"error": "Erreur interne"})
            else:
                self._send(500, "Erreur interne", "text/plain; charset=utf-8")


# ═══════════════════════════════════════════════════════════════
# SURVEILLANCE GMAIL
# ═══════════════════════════════════════════════════════════════
def _decode_mime_header(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _message_body(message) -> str:
    """Return the text body (plain preferred, HTML stripped as fallback)."""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            content_type = (part.get_content_type() or "").lower()
            if content_type not in ("text/plain", "text/html"):
                continue
            try:
                payload = part.get_payload(decode=True) or b""
            except Exception:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                text = payload.decode("utf-8", errors="replace")
            (plain_parts if content_type == "text/plain" else html_parts).append(text)
    else:
        try:
            payload = message.get_payload(decode=True) or b""
        except Exception:
            payload = b""
        charset = message.get_content_charset() or "utf-8"
        try:
            plain_parts.append(payload.decode(charset, errors="replace"))
        except (LookupError, UnicodeDecodeError):
            plain_parts.append(payload.decode("utf-8", errors="replace"))

    if plain_parts:
        return "\n".join(plain_parts)
    html_text = "\n".join(html_parts)
    if not html_text:
        return ""
    html_text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html_text)
    html_text = re.sub(r"(?i)<br\s*/?>", "\n", html_text)
    html_text = re.sub(r"(?i)</p\s*>", "\n", html_text)
    html_text = re.sub(r"<[^>]+>", " ", html_text)
    return html.unescape(html_text)


class EmailOrderWatcher(threading.Thread):
    """Surveille une boîte Gmail et transforme les e-mails en ordres."""

    def __init__(self, service: "RemoteControlService", config: dict, stop_event: threading.Event):
        super().__init__(daemon=True, name="remote-email")
        self.service = service
        self.config = dict(config or {})
        self.stop_event = stop_event

    # ── état persistant (dernier UID traité) ─────────────────
    def _load_state(self) -> dict:
        try:
            if EMAIL_STATE_PATH.exists():
                data = json.loads(EMAIL_STATE_PATH.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    return data
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _save_state(self, state: dict) -> None:
        try:
            EMAIL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            EMAIL_STATE_PATH.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.debug("État e-mail non enregistré: %s", exc)

    # ── boucle ────────────────────────────────────────────────
    def run(self) -> None:
        poll = max(15, int(self.config.get("poll_seconds") or 60))
        state = self._load_state()
        initialized = bool(state.get("initialized"))
        while not self.stop_event.is_set():
            try:
                self._poll_once(state, initialized)
                initialized = True
            except Exception as exc:
                logger.warning("Contrôle par e-mail: %s", exc)
            self.stop_event.wait(poll)

    def _poll_once(self, state: dict, initialized: bool) -> None:
        address = self.config.get("address") or ""
        password = self.config.get("password") or ""
        if not address or not password:
            raise RuntimeError("adresse Gmail ou mot de passe d'application manquant")

        mail = imaplib.IMAP4_SSL(EMAIL_HOST, EMAIL_PORT, timeout=30)
        try:
            mail.login(address, password)
            mail.select("INBOX", readonly=True)
            status, data = mail.uid("search", None, "ALL")
            if status != "OK":
                raise RuntimeError("recherche des messages impossible")
            raw_uids = (data[0] or b"").split() if data else []
            uids = sorted(int(item) for item in raw_uids if item.isdigit())
            max_uid = uids[-1] if uids else 0
            last_uid = int(state.get("last_uid") or 0)

            if not initialized:
                state["last_uid"] = max_uid
                state["initialized"] = True
                self._save_state(state)
                logger.info(
                    "✉️ Contrôle par e-mail : connecté à %s, surveillance des nouveaux messages.",
                    address,
                )
                return

            new_uids = [uid for uid in uids if uid > last_uid]
            for uid in new_uids:
                status, msg_data = mail.uid("fetch", str(uid), "(RFC822)")
                if status != "OK":
                    continue
                raw_message = b""
                for part in msg_data or []:
                    if isinstance(part, tuple) and len(part) >= 2:
                        raw_message = part[1]
                        break
                if raw_message:
                    try:
                        self._process_message(raw_message)
                    except Exception as exc:
                        logger.warning("E-mail %s ignoré: %s", uid, exc)
                state["last_uid"] = max(int(state.get("last_uid") or 0), uid)
                self._save_state(state)
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    def _process_message(self, raw_message: bytes) -> None:
        message = message_from_bytes(raw_message)
        subject = _decode_mime_header(message.get("Subject", ""))
        sender = parseaddr(message.get("From", ""))[1].lower()
        body = _message_body(message)

        allowed = [str(item).lower() for item in (self.config.get("senders") or []) if item]
        if allowed and not any(item in sender for item in allowed):
            logger.info("✉️ E-mail ignoré (expéditeur non autorisé): %s", sender)
            return

        keyword = (self.config.get("keyword") or "record").strip()
        haystack = (subject + "\n" + body).lower()
        if keyword and keyword.lower() not in haystack:
            return

        order = parse_remote_order(body, source="email", default_title=subject)
        order.sender = sender
        order.subject = subject
        self.service.enqueue_order(order)
        logger.info(
            "✉️ Ordre reçu par e-mail de %s : %s (%s)",
            sender or "inconnu", order.meeting_id, order.platform,
        )


def test_email_connection(address: str, password: str) -> tuple[bool, str]:
    """Teste la connexion IMAP Gmail (mot de passe d'application)."""
    address = (address or "").strip()
    password = (password or "").strip()
    if not address or not password:
        return False, "Renseignez l'adresse Gmail et le mot de passe d'application."
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(EMAIL_HOST, EMAIL_PORT, timeout=20)
        mail.login(address, password)
        mail.select("INBOX", readonly=True)
        return True, "Connexion Gmail réussie : la boîte de réception est accessible."
    except imaplib.IMAP4.error as exc:
        return False, (
            f"Connexion Gmail refusée : {exc}\n\n"
            "Vérifiez l'adresse et le mot de passe d'application (compte Google avec "
            "validation en deux étapes, mot de passe de 16 caractères)."
        )
    except Exception as exc:
        return False, f"Connexion Gmail impossible : {exc}"
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════
# SERVICE DE CONTRÔLE À DISTANCE
# ═══════════════════════════════════════════════════════════════
class RemoteControlService:
    """Page web + surveillance e-mail, avec file d'actions pour l'interface."""

    def __init__(
        self,
        web_enabled: bool = False,
        web_port: int = 8080,
        web_token: str = "",
        email_config: Optional[dict] = None,
    ):
        self.web_enabled = bool(web_enabled)
        self.web_port = int(web_port or 8080)
        self.web_token = str(web_token or "")
        self.email_config = dict(email_config or {})
        self.email_enabled = bool(self.email_config.get("enabled"))

        self._actions: "queue.Queue[dict]" = queue.Queue()
        self._httpd: Optional[_RemoteHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None
        self._email_stop = threading.Event()
        self._email_thread: Optional[EmailOrderWatcher] = None
        self._lock = threading.Lock()
        self.last_error = ""
        # Vrai quand une autre instance de l'application sert déjà la page.
        self.web_external = False

        # Callbacks fournis par l'application (thread du serveur HTTP).
        self.status_provider: Optional[Callable[[], dict]] = None
        self.recordings_provider: Optional[Callable[[], list]] = None
        self.download_provider: Optional[Callable[[int], Optional[str]]] = None
        # Source du jeton : lue à chaque requête pour que « Régénérer » ou une
        # autre instance de l'application prenne effet immédiatement.
        self.token_provider: Optional[Callable[[], str]] = None

    def current_token(self) -> str:
        """Jeton effectif (relu depuis les paramètres à chaque appel)."""
        if self.token_provider is not None:
            try:
                value = str(self.token_provider() or "").strip()
                if value:
                    self.web_token = value
            except Exception as exc:  # pragma: no cover - défensif
                logger.debug("Jeton de contrôle à distance illisible: %s", exc)
        return self.web_token

    # ── cycle de vie ─────────────────────────────────────────
    def start(self) -> bool:
        started = False
        if self.web_enabled:
            if not self.current_token():
                logger.warning("Contrôle web activé sans jeton : serveur non démarré.")
            else:
                started = self._start_web() or started
        if self.email_enabled:
            if not (self.email_config.get("address") and self.email_config.get("password")):
                logger.warning(
                    "Contrôle par e-mail activé mais adresse ou mot de passe d'application manquant."
                )
            else:
                self._start_email()
                started = True
        return started

    def stop(self) -> None:
        self._stop_web()
        self._stop_email()

    def _start_web(self) -> bool:
        try:
            self._httpd = _RemoteHTTPServer(("0.0.0.0", self.web_port), _RemoteRequestHandler, self)
        except OSError as exc:
            if _is_our_server(self.web_port):
                # Une autre instance (par ex. le démarrage automatique) sert
                # déjà la page : elle accepte le même jeton, donc tout
                # fonctionne. On l'indique clairement au lieu d'une erreur.
                self.web_external = True
                self.last_error = (
                    f"Page web déjà servie par une autre instance de Zoom Auto Recorder "
                    f"sur le port {self.web_port} (même jeton). Fermez l'autre instance "
                    f"ou choisissez un autre port."
                )
                logger.warning("🌐 %s", self.last_error)
            else:
                self.last_error = f"Port {self.web_port} indisponible : {exc}"
                logger.error("🌐 Page web de contrôle : %s", self.last_error)
            return False
        self.web_external = False
        self._http_thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name="remote-http"
        )
        self._http_thread.start()
        logger.info("🌐 Page web de contrôle active sur le port %s.", self.web_port)
        for url in self.web_urls():
            logger.info("   %s", url)
        return True

    def _stop_web(self) -> None:
        self.web_external = False
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        if self._http_thread is not None:
            try:
                self._http_thread.join(timeout=3)
            except Exception:
                pass
            self._http_thread = None

    def _start_email(self) -> None:
        self._email_stop.clear()
        self._email_thread = EmailOrderWatcher(self, self.email_config, self._email_stop)
        self._email_thread.start()
        logger.info(
            "✉️ Contrôle par e-mail actif : %s (mot-clé « %s », toutes les %s s).",
            self.email_config.get("address"),
            self.email_config.get("keyword") or "record",
            self.email_config.get("poll_seconds") or 60,
        )

    def _stop_email(self) -> None:
        self._email_stop.set()
        thread = self._email_thread
        self._email_thread = None
        if thread is not None:
            try:
                thread.join(timeout=5)
            except Exception:
                pass

    # ── état ─────────────────────────────────────────────────
    def is_web_running(self) -> bool:
        return self._httpd is not None

    def is_email_running(self) -> bool:
        return self._email_thread is not None and self._email_thread.is_alive()

    def web_urls(self, include_token: bool = True) -> list[str]:
        urls: list[str] = []
        suffix = ""
        token = self.current_token() if include_token else ""
        if token:
            suffix = "/?token=" + token
        else:
            suffix = "/"
        for host in _local_hosts():
            urls.append(f"http://{host}:{self.web_port}{suffix}")
        return urls

    # ── file d'actions (thread GUI) ──────────────────────────
    def enqueue_order(self, order: RemoteOrder) -> None:
        self._actions.put({"type": "order", "order": order})

    def enqueue_stop(self) -> None:
        self._actions.put({"type": "stop"})

    def poll(self) -> list[dict]:
        actions: list[dict] = []
        while True:
            try:
                actions.append(self._actions.get_nowait())
            except queue.Empty:
                break
        return actions

    # ── données exposées au serveur ──────────────────────────
    def get_status(self) -> dict:
        base = {
            "app": "ZoomAutoRecorder",
            "server_time": datetime.now().isoformat(timespec="seconds"),
            "web_enabled": self.web_enabled,
            "web_running": self.is_web_running(),
            "email_enabled": self.email_enabled,
            "email_running": self.is_email_running(),
            "web_port": self.web_port,
        }
        if self.status_provider is not None:
            try:
                extra = self.status_provider() or {}
                if isinstance(extra, dict):
                    base.update(extra)
            except Exception as exc:
                base["last_error"] = f"État indisponible : {exc}"
        if self.last_error and not base.get("last_error"):
            base["last_error"] = self.last_error
        return base

    def get_recordings(self) -> list:
        if self.recordings_provider is None:
            return []
        try:
            items = self.recordings_provider() or []
            return list(items)
        except Exception as exc:
            logger.debug("Liste des enregistrements indisponible: %s", exc)
            return []

    def get_download_path(self, recording_id) -> Optional[str]:
        if self.download_provider is None:
            return None
        try:
            return self.download_provider(int(recording_id))
        except (TypeError, ValueError):
            return None
        except Exception as exc:
            logger.debug("Téléchargement indisponible: %s", exc)
            return None


def _local_hosts() -> list[str]:
    """Noms/IP locales utilisables depuis le téléphone (Tailscale inclus)."""
    hosts: set[str] = set()
    try:
        hostname = socket.gethostname()
        if hostname:
            hosts.add(hostname)
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            address = info[4][0]
            if address and ":" not in address and not address.startswith("127."):
                hosts.add(address)
    except Exception:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            address = probe.getsockname()[0]
            if address and not address.startswith("127."):
                hosts.add(address)
        finally:
            probe.close()
    except Exception:
        pass
    return sorted(hosts)
