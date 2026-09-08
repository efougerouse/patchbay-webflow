# Patchbay Webflow

Gestionnaire de connexions MCP Webflow pour Claude Code, sous Linux et macOS.
Équivalent libre du principe d'[AgencyFlow](https://www.agencyflowmcp.com/) (macOS) :
**un serveur MCP nommé par projet, chaque token OAuth scopé à un seul site Webflow,
toutes les connexions vivantes en parallèle** — plus de déconnexion/reconnexion
au changement de projet.

## Pourquoi ça marche

Claude Code indexe les credentials MCP par `nom-du-serveur|sha256({type,url,headers})[0:16]`.
Deux serveurs de noms différents pointant la même URL (`https://mcp.webflow.com/mcp`)
ont donc deux tokens OAuth indépendants, stockés dans `~/.claude/.credentials.json`
(Linux) ou dans le trousseau macOS (entrée « Claude Code-credentials »).
Aucune app n'est nécessaire pour la mécanique : Patchbay n'est que le confort autour.

## Contenu

| Fichier | Rôle |
| --- | --- |
| `app.py` | Serveur local (Python stdlib, zéro dépendance) + fenêtre `google-chrome --app` |
| `index.html` | L'interface — la « baie » : un jack par projet, bleu = autorisé, ambre = en attente |
| `icon.svg` | Icône de l'app |
| `wfmcp` | Équivalent CLI (`wfmcp add / ls / check / rm`) |
| `patchbay-webflow.desktop` | Lanceur Linux (menu d'applications + Bureau) |
| `install-macos.sh` | Construit le bundle `~/Applications/Patchbay Webflow.app` et installe `wfmcp` |

## Installation

### Linux

```bash
git clone <repo> ~/.local/share/patchbay-webflow
cp ~/.local/share/patchbay-webflow/wfmcp ~/.local/bin/ && chmod +x ~/.local/bin/wfmcp
cp ~/.local/share/patchbay-webflow/patchbay-webflow.desktop ~/.local/share/applications/
cp ~/.local/share/patchbay-webflow/patchbay-webflow.desktop ~/Bureau/
chmod +x ~/Bureau/patchbay-webflow.desktop
gio set ~/Bureau/patchbay-webflow.desktop metadata::trusted true
```

Les chemins du `.desktop` sont absolus (`/home/efougerouse/…`) : à adapter sur une autre machine.

### macOS

```bash
git clone <repo> ~/.local/share/patchbay-webflow
~/.local/share/patchbay-webflow/install-macos.sh
```

Puis double-clic sur **Patchbay Webflow** dans `~/Applications` (ou Spotlight). Prérequis : `python3`
(outils de ligne de commande Xcode ou Homebrew), `jq` pour `wfmcp check`, Google Chrome pour la
fenêtre en mode app (sinon le navigateur par défaut s'ouvre). Les projets sont cherchés dans
`~/Desktop/Webflow` (le dossier que le Finder affiche « Bureau »).

Ce qui change sous macOS, et ce que le système demande au premier lancement :

- Les tokens MCP sont lus dans le **trousseau** (`security find-generic-password -s "Claude Code-credentials"`),
  le fichier `.credentials.json` servant de repli → macOS demande l'accès au trousseau pour `python3`
  et pour `security` : répondre **Toujours autoriser**.
- « Ouvrir le terminal ici » passe par **Terminal.app** via AppleScript → macOS demande l'autorisation
  de contrôler Terminal (Réglages → Confidentialité → Automatisation).
- Le lanceur est un shell de login (`zsh -l`) : le PATH vient de `~/.zprofile`. Si `claude` n'est
  installé que via nvm (PATH ajouté dans `~/.zshrc`), le déplacer dans `~/.zprofile`.

## Utilisation

1. Ouvrir Patchbay → **＋ Brancher un projet** → choisir le dossier → **Brancher**
2. Cliquer le jack ambre → **Ouvrir le terminal ici** → dans Claude Code : `/mcp` → `wf-<projet>` → Authenticate
3. Dans le navigateur : cocher **uniquement** le site du projet → Authorize — une seule fois, pour toujours

Les tokens ne quittent jamais la machine ; le serveur écoute sur 127.0.0.1 avec un
chemin secret par session et s'éteint seul à la fermeture de la fenêtre.
