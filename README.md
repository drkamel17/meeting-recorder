# 🎥 Zoom Auto Recorder & Meeting Assistant
## Documentation Complète

---

## 📦 Installation

```powershell
cd "<dossier-du-projet>"
pip install -r requirements.txt
```

Exemple :

```powershell
cd "C:\Deploy\ZoomAutoRecorder"
```

## 🚀 Lancement

```powershell
python main.py
```

Ou double-cliquez sur `ZoomAutoRecorder.exe` après construction.

Modes spéciaux :

| Commande | Effet |
|----------|-------|
| `python main.py --remote-server` | Démarre en **contrôle à distance** (fenêtre réduite, prête à recevoir un ordre web/e-mail) |
| `python main.py --run-meeting <id>` | Lance une réunion planifiée (utilisé par le Planificateur Windows) |
| `python main.py --run-meeting-headless <id>` | Ancien mode sans interface (avancé) |

## 🏗️ Structure du projet

```
auto zoom/
├── main.py                    # Point d'entrée
├── requirements.txt           # Dépendances
├── build.bat                  # Script PyInstaller
├── README.md
├── KamelRec-1.0.0-Portable.exe
├── src/
│   ├── __init__.py
│   ├── config.py              # Constantes et chemins dynamiques
│   ├── logger.py              # Logging
│   ├── security.py            # Chiffrement portable
│   ├── settings.py            # Paramètres persistants (enregistreur, contrôle à distance)
│   ├── database.py            # SQLite CRUD
│   ├── scheduler.py           # Planificateur
│   ├── automation.py          # Moteur Zoom
│   ├── kamelrec.py            # Pilote KamelRec
│   ├── windows_tasks.py       # Tâches planifiées Windows + démarrage auto
│   ├── remote_control.py      # Contrôle à distance (page web + Gmail)
│   ├── clipboard.py           # Copie fiable dans le presse-papiers (Win32)
│   ├── scheduled_runner.py    # Mode sans interface (avancé)
│   ├── debug_logger.py        # Debug tools
│   ├── app.py                 # Interface principale
│   └── ui/
│       ├── add_meeting_dialog.py
│       └── windows_tasks_dialog.py
├── data/
│   └── zoom_auto_recorder.db  # Base SQLite
└── logs/
    └── app.log                # Journal
```

## 🎙️ Choix de l'enregistreur

Onglet **⚙️ Paramètres** → **Enregistreur vidéo** :

| Enregistreur | Pause/Reprise | Remarques |
|--------------|---------------|-----------|
| **KamelRec** (par défaut) | Oui | Nécessite `KamelRec-1.0.0-Portable.exe` à côté de l'application |
| **Windows Game Bar** | Non | Utilise `Win+Alt+R`, puis déplace le MP4 de `Vidéos\Captures` vers le dossier des enregistrements |

Le choix est enregistré dans `data/settings.json` (ou `%LOCALAPPDATA%\ZoomAutoRecorder\data\settings.json` en mode compilé) et s'applique au prochain enregistrement, y compris avec les tâches Windows.

Pour la Game Bar, activez **Paramètres Windows → Jeux → Game Bar** (ou Xbox Game Bar) et l'enregistrement des clips de jeu (y compris l'audio).

## 🪟 Fenêtres pendant l'enregistrement

Pendant tout l'enregistrement, l'application surveille les fenêtres qui s'ouvrent au-dessus de la réunion :

- **Boîtes d'alerte et dialogues** (messages d'enregistrement, notifications, popups) : fermés dès leur apparition.
- **Messages sans titre** (par exemple l'avis Zoom « Cette réunion est enregistrée ») : détectés et fermés, même quand la fenêtre n'a pas de titre.
- **Messages affichés à l'intérieur de la fenêtre Zoom** (avec un bouton OK / D'accord) : le bouton est cliqué automatiquement toutes les 2 secondes.
- **Fenêtres normales** (navigateur, explorateur, etc.) : réduites dans la barre des tâches.
- **Jamais touchés** : la fenêtre de la réunion, l'application elle-même et KamelRec.

La fenêtre de la réunion est remise au premier plan après chaque intervention, et le plein écran Google Meet est vérifié en continu.

L'option est **activée par défaut**. Elle se désactive dans **⚙️ Paramètres → 🪟 Fenêtres pendant l'enregistrement** (clé `close_popups_during_recording` dans `data/settings.json`).

### Intervenir pendant l'enregistrement

Deux boutons dans une **barre de garde permanente**, sous l'en-tête, permettent de reprendre la main sans arrêter la vidéo. Cette barre est **toujours affichée**, même quand aucun enregistrement n'est en cours, avec un indicateur d'état :

- 🛡 **Garde : inactive (aucun enregistrement)** — gris
- 🛡 **Garde : ACTIVE — fenêtres fermées** — vert
- ⏸ **Garde : SUSPENDUE — fenêtres ouvertes** — orange

| Bouton | Effet |
|--------|-------|
| **⏸ Suspendre la garde** | Arrête la surveillance : les fenêtres qui s'ouvrent restent ouvertes (utile pour répondre à une invite, ouvrir un document…). |
| **🛡 Réactiver la garde** | Relance immédiatement la surveillance, même si l'option est décochée dans les paramètres. |

Les deux boutons restent **visibles et cliquables en permanence**, y compris après l'arrêt de l'enregistrement ; hors enregistrement, un clic est simplement journalisé sans effet. La suspension reste active jusqu'à la réactivation ou jusqu'à la fin de l'enregistrement (une nouvelle réunion redémarre la garde).

**Raccourci clavier : `Ctrl+S`** — suspend la garde, et appuie à nouveau pour la réactiver. Le raccourci est **global** : il fonctionne même quand la réunion est au premier plan (plein écran). Il n'est actif que pendant l'enregistrement et il est libéré à l'arrêt, pour ne pas gêner les autres applications.

## 🎯 Fonctionnalités

| Module | Description |
|--------|-------------|
| **Tableau de bord** | Liste des réunions + journal en temps réel |
| **Gestion des réunions** | CRUD complet avec paramètres avancés |
| **Planificateur** | Vérification auto toutes les 10s |
| **Automatisation Zoom** | Lancement, join, passcode, enregistrement |
| **Automatisation Google Meet** | Lien meet.google.com, navigateur, micro/caméra, enregistrement |
| **Enregistreur au choix** | KamelRec ou Windows Game Bar |
| **Fenêtres parasites** | Fermées ou réduites automatiquement pendant l'enregistrement (Ctrl+S pour suspendre) |
| **Contrôle à distance** | Page web protégée par jeton + déclenchement par e-mail Gmail |
| **Chiffrement** | Mots de passe protégés (clé portable) |
| **Debug** | Captures écran + dump UI automatique |

## 📋 Utiliser l'application

### Zoom
1. **Lancer** : `python main.py`
2. **Ajouter une réunion** : Onglet "Réunions" → "Ajouter"
3. **Remplir les champs** :
   - Nom de la réunion
   - Meeting ID / Lien
   - Mot de passe (optionnel)
   - Date/heure de début
   - Durée en minutes
   - Nom affiché (Display Name)
4. **Activer la réunion** : Toggle "Activer/Désactiver"
5. **Lancer manuellement** : Bouton "Démarrer maintenant"

### Google Meet
1. **Lancer** : `python main.py`
2. **Ajouter une réunion** : Onglet "Réunions" → "Ajouter"
3. **Sélectionner "Google Meet"** dans le champ Plateforme
4. **Remplir les champs** :
   - Nom de la réunion
   - Lien Google Meet complet (ex: `https://meet.google.com/abc-defg-hij`)
   - Date/heure de début
   - Durée en minutes
   - Nom affiché (optionnel)
5. **Activer la réunion** : Toggle "Activer/Désactiver"
6. **Lancer manuellement** : Bouton "Démarrer maintenant"

> 💡 Le micro et la caméra sont coupés par défaut avant de rejoindre la réunion Google Meet.

## 🔧 Construction EXE

```powershell
build.bat
```
Résultat : `dist\ZoomAutoRecorder.exe`

## 🪟 Planification Windows (application fermée)

L’application crée automatiquement une tâche Windows pour chaque réunion **activée** et en attente (`Waiting`).

- Tâche : `ZoomAutoRecorder-Meeting-<id>`
- Déclencheur : date et heure de la réunion
- Action : `pythonw.exe main.py --run-meeting <id>` (ou `ZoomAutoRecorder.exe --run-meeting <id>`)
- La tâche est créée, modifiée, activée/désactivée et supprimée automatiquement depuis l’application.
- Les tâches sont liées à l’utilisateur Windows actuel et nécessitent une session **connectée et déverrouillée**.
- Un PC verrouillé, en veille ou sans session ouverte ne pourra pas piloter Zoom.
- La fenêtre de démarrage acceptée est de **5 minutes** après l’heure prévue.
- La réunion est revendiquée atomiquement pour éviter qu’elle démarre deux fois (application ouverte + tâche Windows).

### Ce qui se passe au déclenchement

Au lieu de tourner sans interface, la tâche Windows ouvre **l’interface graphique de l’application, réduite dans la barre des tâches** :

1. Zoom (ou le navigateur pour Google Meet) démarre et rejoint la réunion.
2. L’enregistrement commence automatiquement.
3. À la fin de la durée prévue — ou si vous cliquez sur **⏹ Arrêter** — l’enregistrement s’arrête et **l’application reste ouverte**, réduite dans la barre des tâches.
4. Vous pouvez alors restaurer la fenêtre pour vérifier l’enregistrement, puis **fermer l’application** vous-même (croix de la fenêtre).

La réunion terminée n’est **pas relancée automatiquement** : vous pouvez la relancer à tout moment avec **▶ Lancer** depuis l’onglet Réunions. Le raccourci **Ctrl+S** (suspendre/réactiver la garde) reste disponible pendant l’enregistrement, même fenêtre réduite.

> Mode avancé : `ZoomAutoRecorder.exe --run-meeting-headless <id>` conserve l’ancien fonctionnement sans interface (aucune fenêtre, arrêt automatique à la fin).

## 🌐 Contrôle à distance (téléphone ou e-mail)

L'application peut **s'ouvrir toute seule**, rejoindre une réunion Zoom ou Google Meet et démarrer l'enregistrement à partir d'un ordre envoyé depuis un téléphone ou par e-mail. Deux canaux sont disponibles et peuvent fonctionner **en même temps** :

1. **Page web** — un formulaire accessible depuis un navigateur (téléphone, autre PC).
2. **E-mail Gmail** — un e-mail contenant un lien Zoom / Meet déclenche l'enregistrement.

Tout se configure dans **⚙️ Paramètres → 🌐 Contrôle à distance (page web + e-mail Gmail)**.

### 1. Page web de contrôle

- Cochez **« Activer la page web de contrôle à distance »**.
- Choisissez un **port** (8080 par défaut) et conservez le **jeton d'accès** généré automatiquement.
- Trois boutons facilitent la récupération de l'accès :
  - **📋 Copier le jeton** — copie le jeton seul (police à chasse fixe, `I` et `l` restent distinguables) ;
  - **📋 Copier le lien complet** — copie l'adresse entière, jeton inclus ;
  - **🌐 Ouvrir la page de contrôle** — ouvre la page dans le navigateur du PC.
- Cliquez sur **💾 Enregistrer le contrôle à distance**. L'application affiche alors les adresses utilisables, par exemple :

```text
http://<nom-du-PC>:8080/?token=XXXXXXXXXXXX
```

- Ouvrez cette adresse depuis le téléphone : un formulaire permet de saisir
  - le **lien Zoom / Google Meet** ou l'**ID** de réunion (9 à 12 chiffres),
  - le **code secret** (passcode),
  - le **nom affiché**,
  - le **nom de la réunion** et la **durée** (minutes),
  - éventuellement une **heure de début** (sinon l'enregistrement démarre immédiatement).

La page affiche aussi l'**état** (réunion en cours, durée), permet d'**arrêter** l'enregistrement et de **télécharger** les derniers enregistrements.

> 🔒 Le jeton est obligatoire : sans lui, la page s'ouvre mais aucune action ni donnée n'est accessible. Le bouton **🔄 Régénérer** crée un nouveau jeton (l'ancien lien cesse de fonctionner).

#### Saisie du jeton : tolérances et confort

- Le jeton est comparé **sans tenir compte de la casse** et en neutralisant les confusions visuelles les plus fréquentes : `I` (i majuscule) = `l` (L minuscule) = `1`, et `O` = `0`. Un jeton lu à l'écran ou recopié à la main fonctionne donc même si un caractère a été confondu.
- Les **nouveaux jetons** sont générés dans un alphabet sans caractères ambigus (`abcdefghjkmnpqrstuvwxyz23456789`, 24 caractères : pas de `i`, `l`, `o`, `0`, `1`).
- Le jeton saisi est **mémorisé dans le navigateur** (`localStorage`) : il n'est plus nécessaire de le retaper à chaque visite.
- Le formulaire est envoyé **sans rechargement de page** (AJAX) : en cas d'erreur, les champs déjà remplis (lien, code, nom) sont conservés et le jeton saisi reste dans le champ.
- Le serveur accepte le corps du formulaire en `application/x-www-form-urlencoded` **et** en `multipart/form-data` : une page déjà ouverte dans le navigateur continue donc de fonctionner après une mise à jour de l'application.
- Si le jeton du lien est refusé, un bandeau explicite s'affiche au lieu d'une page blanche.
- Si le port est déjà utilisé par une **autre instance** de l'application, la page reste accessible (même jeton) et les paramètres l'indiquent : « Page web : servie par une autre instance ».

### 2. Déclenchement par e-mail Gmail

- Cochez **« Activer le déclenchement par e-mail (Gmail IMAP) »**.
- Renseignez l'**adresse Gmail**, un **mot de passe d'application** et un **mot-clé** (par défaut `record`).
- **Expéditeurs autorisés** (optionnel) : liste d'adresses séparées par des virgules. Vide = tous les expéditeurs.
- **Vérification toutes les** : intervalle de lecture de la boîte (60 s par défaut).
- Cliquez sur **✉️ Tester la connexion Gmail** pour valider.

Le **mot de passe d'application** se crée dans le compte Google (nécessite la validation en deux étapes) :

```text
Compte Google → Sécurité → Validation en deux étapes → Mots de passe des applications
```

Il fait 16 caractères, sans espaces. Il est chiffré dans les paramètres (clé portable) et n'apparaît jamais en clair.

Exemple d'e-mail (l'objet ou le corps doit contenir le mot-clé) :

```text
Objet : record

https://zoom.us/j/1234567890?pwd=abc123
Nom : Kamel
Réunion : Consultation
Durée : 45
```

Les libellés **français, anglais et arabe** sont acceptés (`Nom/Name/الاسم`, `Mot de passe/Passcode/كلمة المرور`, `Réunion/Meeting/اسم الاجتماع`, `Durée/Duration/المدة`, `Début/Start/الوقت`…).

> Les messages déjà présents au premier démarrage sont ignorés : seuls les **nouveaux** e-mails déclenchent un enregistrement. Chaque message n'est traité qu'une seule fois.

### 3. Démarrage automatique et fermeture de la fenêtre

Pour que le PC soit joignable **même quand l'application n'est pas ouverte à l'écran**, le contrôle à distance fonctionne en arrière-plan :

- **Fermer la fenêtre ne coupe pas le serveur.** Quand la page web (ou l'e-mail) est activée, cliquer sur la croix masque la fenêtre : l'application continue de tourner, une **icône apparaît près de l'horloge** et la page reste accessible. Un clic droit sur l'icône permet de **rouvrir** la fenêtre ou de **quitter complètement** (ce qui arrête le contrôle à distance).
- **Démarrage automatique avec Windows.** Dès que la page web est activée, un raccourci est créé automatiquement dans le dossier **Démarrage** de l'utilisateur :
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ZoomAutoRecorder-RemoteServer.lnk`
  Il lance l'application en mode `--remote-server` : **aucune fenêtre dans la barre des tâches**, seulement l'icône près de l'horloge, et la page web est active immédiatement après l'ouverture de session. La fenêtre se restaure dès qu'un ordre arrive.
- La case **« Démarrer le contrôle à distance à l'ouverture de session Windows »** (⚙️ Paramètres) permet de désactiver ce comportement ; si vous la décochez, votre choix est conservé.
- Ce mode ne nécessite **aucun droit administrateur**.
- Un verrou Windows empêche de lancer deux serveurs de contrôle à distance en même temps.

> 💡 Pour arrêter vraiment tout (et rendre le lien inactif), utilisez **clic droit sur l'icône → « Quitter complètement »**. Un simple clic sur la croix ne suffit plus, c'est voulu.

### 4. Accès depuis Internet

La page écoute sur toutes les interfaces (`0.0.0.0`). Trois options, de la plus simple à la plus robuste :

| Méthode | Utilisation |
|---------|-------------|
| **Réseau local** | `http://<nom-du-PC>:8080/?token=...` depuis un téléphone sur le même Wi-Fi |
| **Tailscale** (recommandé) | Installez Tailscale sur le PC **et** sur le téléphone, puis ouvrez `http://<nom-tailscale-du-PC>:8080/?token=...` — aucun port à ouvrir sur la box |
| **Cloudflare Tunnel** | `cloudflared tunnel --url http://localhost:8080` puis utilisez l'URL HTTPS fournie (ajoutez `?token=...`) |

> ⚠️ **N'exposez jamais le port 8080 directement sur Internet sans HTTPS ni jeton.** Préférez Tailscale ou un tunnel Cloudflare.

### 5. Ce qui se passe à la réception d'un ordre

1. La fenêtre de l'application (dans la zone de notification) est **restaurée**.
2. Une réunion est créée dans la base avec le lien, le code et le nom fournis.
3. Zoom (ou le navigateur pour Google Meet) est lancé, la réunion est rejointe, puis **l'enregistrement démarre**.
4. Si une **heure de début future** est fournie, la réunion est planifiée et une tâche Windows est créée (elle démarrera même si l'application est fermée).
5. Si une réunion est **déjà en cours**, l'ordre est ignoré et l'événement est journalisé.

### 6. Prérequis indispensables

- Le **PC doit rester allumé**, connecté à Internet et la **session Windows ouverte et déverrouillée** (pas d'écran de verrouillage, pas de veille).
- **N'utilisez pas le Bureau à distance (RDP)** pour piloter ce PC : la connexion RDP verrouille la session interactive dont Zoom a besoin. Utilisez Tailscale + la page web, ou un accès physique.
- Désactivez la mise en veille :

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

- Autorisez l'application dans le **Pare-feu Windows** si une invite apparaît pour le port choisi.
- Le PC doit pouvoir **démarrer Zoom et cliquer dans l'interface** : aucune session « service » ni « tâche déconnectée » ne fonctionne.

### 7. Dépannage du contrôle à distance

| Problème | Solution |
|----------|----------|
| Page inaccessible | Vérifier que la page web est cochée et enregistrée, que le port est libre, et le pare-feu Windows |
| Page inactive après avoir fermé la fenêtre | Depuis cette version, la croix masque la fenêtre et le serveur continue : vérifier l'icône près de l'horloge. Si « Quitter complètement » a été utilisé, relancer l'application |
| « Jeton invalide » | Utiliser **📋 Copier le jeton** (presse-papiers fiable) puis **📋 Copier le lien complet**, ou rouvrir le lien avec `?token=...`. La comparaison tolère la casse et les confusions `I`/`l`/`1`, `O`/`0` |
| E-mail non pris en compte | Vérifier l'adresse, le mot de passe d'application (16 caractères), le mot-clé et l'expéditeur autorisé ; utiliser « Tester la connexion Gmail » |
| Rien ne se lance | Vérifier `logs/app.log` : ordre ignoré (réunion déjà en cours), lien/ID non reconnu, ou session Windows verrouillée |
| Le PC ne répond plus après un moment | Mise en veille ou verrouillage : appliquer les commandes `powercfg` ci-dessus |

## 🛠️ Déploiement sur un autre PC

1. Construisez l’application :

```powershell
build.bat
```

2. Copiez **tout** le dossier `dist` sur l’autre PC. Il contient :

```text
ZoomAutoRecorder.exe
KamelRec-1.0.0-Portable.exe
```

3. Lancez `ZoomAutoRecorder.exe` une fois. Les tâches Windows seront synchronisées automatiquement.

4. Si vous déplacez l’exécutable après avoir créé des tâches, rouvrez simplement l’application : les tâches sont recréées avec le nouveau chemin.

### Données utilisateur

- Si un dossier `data/zoom_auto_recorder.db` existe à côté de l’exécutable, l’installation est conservée en mode portable.
- Sinon, les données sont stockées dans :

```text
%LOCALAPPDATA%\ZoomAutoRecorder\data
%LOCALAPPDATA%\ZoomAutoRecorder\logs
```

Ainsi, l’application fonctionne aussi depuis un dossier protégé comme `Program Files`.

### Compatibilité des mots de passe

Les nouveaux mots de passe sont chiffrés avec une clé portable de l’application. Les anciens mots de passe chiffrés avec la clé spécifique de ce PC sont toujours lisibles automatiquement.

## ⚠️ Notes importantes

### Limitations Zoom gratuit
- Les réunions gratuites limitent à **40 minutes**
- L'enregistrement local nécessite un compte **Pro/Business**
- L'hôte peut désactiver l'enregistrement

### Classes de fenêtres Zoom détectées
- `ZPPTEnhanceLoginMainWindowClassEx` — Fenêtre principale
- `zWaitingMeetingIDWndClass` — Dialogues Join/Passcode
- `ConfMultiTabContentWndClass` — Réunion active
- `VideoPreviewWndClass` — Aperçu vidéo

### Raccourcis clavier
- `Ctrl+Shift+S` — Démarrer/Arrêter l'enregistrement local Zoom
- `Ctrl+Shift+P` — Ancien raccourci pause/reprise (KamelRec utilise ses propres boutons)

## 🐛 Dépannage

| Problème | Solution |
|----------|----------|
| Zoom non détecté | Vérifier l'installation de Zoom (dossiers standard) ou définir `ZOOM_AUTORECORDER_ZOOM_EXE` |
| Game Bar ne démarre pas | Activer Game Bar et « Enregistrer les clips de jeu » dans Windows |
| Boutons non trouvés | Consulter `data/debug/` pour les captures |
| Erreur import | `pip install -r requirements.txt` |
| DB verrouillée | Fermer l'application et réouvrir |

## 📞 Support

Logs disponibles dans :
- `logs/app.log` — Journal complet
- `data/debug/` — Captures écran de débogage

---

*Version 1.0.0 — Novembre 2026*
