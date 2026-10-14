# playlist2slsk

Playlist Spotify → fichiers audio rangés, via Soulseek.

S'appuie sur **sockseek** (anciennement sldl / slsk-batchdl) pour la recherche et le téléchargement, et ajoute :

- **le renommage** selon le nom Spotify (`Artiste - Titre.ext` par défaut, modèle configurable) ;
- **les tags réécrits** (titre, artiste, album, année, n° de piste) à partir des métadonnées Spotify, plutôt que les tags parfois fantaisistes des fichiers partagés ;
- **la jaquette intégrée** au fichier (MP3, FLAC, M4A, OGG/Opus, WAV/AIFF), en 640 px quand c'est possible ;
- **une playlist `.m3u8`** dans l'ordre d'origine ;
- **un `missing.csv`** listant les morceaux introuvables ;
- **la reprise** : relancer la même commande ne retélécharge pas ce qui est déjà là.

## Installation (macOS)

1. **sockseek** : télécharge la version macOS sur https://github.com/fiso64/sockseek/releases, puis :
   ```bash
   chmod +x sockseek
   xattr -d com.apple.quarantine sockseek   # sinon macOS bloque le binaire
   sudo mv sockseek /usr/local/bin/
   ```
2. **Config Soulseek** dans `~/.config/sockseek/sockseek.conf` (idéalement un compte dédié, pas celui de ton client habituel) :
   ```
   username = ton-compte-slsk
   password = ton-mot-de-passe
   ```
   Le script ne voit jamais ces identifiants.
3. **Dépendances Python** :
   ```bash
   pip3 install -r requirements.txt
   ```

## Interface graphique

Double-clic sur **`Lancer.command`** (ou `python3 app.py`) : la page s'ouvre dans ton navigateur. Tout tourne sur ton Mac, le serveur n'écoute que sur `127.0.0.1`.

Dépose le CSV (ou colle un lien Spotify, YouTube ou SoundCloud), vérifie la liste, choisis la qualité et le dossier, puis clique sur *Télécharger*. Chaque morceau passe au vert ou au rouge, et un bouton relance les manquants en recherche élargie. Tes réglages sont gardés dans `~/.playlist2slsk/`.

Aussi dans l'interface :
- **Corriger un introuvable** : clic sur un morceau en rouge, retouche artiste ou titre, *Chercher à nouveau*. La correction est retenue.
- **Faux FLAC** : chaque FLAC est analysé (spectre). Les suspects sont signalés et un bouton les remplace par une autre source.
- **Déjà dans ta bibliothèque** : Réglages › Fichiers, indique tes dossiers de musique pour ne pas retélécharger ce que tu as.
- **Traktor et Rekordbox** : Réglages › DJ crée un `.nml` par playlist et tient à jour un `rekordbox.xml`.
- **Spotify sans compte** : colle le lien d'une playlist publique ou d'un album (jusqu'à 100 titres). Pour une playlist privée, plus longue ou tes titres likés, passe par Exportify : le CSV est récupéré automatiquement dans Téléchargements.
- **Playlists suivies** : sur un lien Spotify, YouTube ou SoundCloud, *+ Suivre*. L'onglet *Playlists* met à jour une ou toutes les playlists en ne téléchargeant que les nouveaux morceaux, éventuellement à chaque ouverture.

> Au premier lancement, macOS peut bloquer `Lancer.command` : clic droit → *Ouvrir*. Le premier lancement crée un environnement Python `.venv` dans le dossier et y installe les dépendances. S'il n'est pas exécutable : `chmod +x Lancer.command`.

## Utilisation en ligne de commande

### 1. Récupérer la playlist

Le plus simple est d'utiliser **https://exportify.app** : connexion Spotify, puis bouton *Export* sur la playlist, ce qui donne un CSV. Ça marche aussi avec les exports TuneMyMusic ou Soundiiz, les colonnes sont détectées automatiquement.

L'URL Spotify directe fonctionne aussi, mais elle demande une app développeur Spotify (`--spotify-id` / `--spotify-secret`), et Spotify bloque souvent la lecture des playlists en « Development Mode ». Le CSV évite ce problème.

### 2. Lancer

```bash
# Vérifier la liste et la commande sans rien télécharger
python3 playlist2slsk.py ~/Downloads/ma_playlist.csv --dry-run

# Lancer pour de vrai, en préférant le FLAC (repli en MP3 si absent)
python3 playlist2slsk.py ~/Downloads/ma_playlist.csv --lossless
```

Résultat dans `~/Music/playlist2slsk/<nom de la playlist>/`.

### Options utiles

| Option | Effet |
|---|---|
| `-o DOSSIER` | dossier racine (défaut `~/Music/playlist2slsk`) |
| `--name NOM` | nom du sous-dossier |
| `--pattern "…"` | modèle de nom : `{artist}` `{artist1}` `{title}` `{album}` `{year}` `{n}`. Ex. `"{n} - {artist} - {title}"` ou `"{album}/{artist} - {title}"` |
| `--lossless` | préférer FLAC/WAV |
| `--only-lossless` | FLAC obligatoire |
| `-n 10` | seulement les 10 premiers (pour tester) |
| `--no-cover` / `--no-tags` | désactiver jaquette / tags |
| `-- …` | tout ce qui suit est passé à sockseek |

### Quand des morceaux manquent

Relance **la même commande** (le déjà-téléchargé est ignoré) en élargissant la recherche :

```bash
python3 playlist2slsk.py ma_playlist.csv --lossless -- --length-tol 10 --desperate
```

Si au contraire de **mauvais morceaux** sont téléchargés, tu peux resserrer :

```bash
-- --strict-title --strict-artist
```

## Comment ça marche

1. Le script écrit un CSV de travail (`Artist, Title, Album, Length, URI`) et lance
   `sockseek <csv> -o <dossier temp> --name-format "{snum}"`, ce qui donne `1.flac`, `2.mp3`, etc. Le numéro correspond à la ligne, donc chaque fichier est rattaché à son morceau Spotify sans deviner.
2. Chaque fichier est déplacé et renommé, ses tags sont réécrits et la jaquette est intégrée. La jaquette est cherchée dans l'ordre suivant : URL du CSV ou de l'API, oEmbed Spotify (public, sans clé), puis iTunes Search.
3. La `.m3u8` et le `missing.csv` sont écrits, puis le dossier temporaire est supprimé.

Seul l'artiste principal est envoyé à la recherche (meilleurs résultats). Tous les artistes figurent dans le nom de fichier et les tags.

## Limites

- sockseek ne partage pas de fichiers. Pour jouer le jeu du réseau, partage ta collection avec un vrai client (Nicotine+, slskd).
- Soulseek limite le nombre de recherches. sockseek gère ça tout seul, mais une playlist de 500 titres prend du temps.
- Rappel légal : télécharger de la musique protégée que tu n'as pas achetée reste de la contrefaçon en France.

## Appli installable (Mac et Windows)

GitHub Actions construit les installateurs à chaque envoi sur `main` (onglet **Actions** › dernière exécution › *Artifacts*) :

- `Playlist2Slsk-Mac-AppleSilicon.dmg` (Mac M1/M2/M3…) et `Playlist2Slsk-Mac-Intel.dmg`
- `Playlist2Slsk-Windows-Setup.exe`

Python, sockseek et l'interface sont inclus : rien d'autre à installer. Le point d'entrée est `desktop.py` (fenêtre pywebview) et la recette est dans `packaging/build.sh`.

Pour publier une version : mettre le numéro dans `VERSION`, puis créer le tag correspondant (`v1.0.0`, `v1.1.0`…). Les installateurs sont joints à une Release, et les applis déjà installées le signalent au lancement (elles interrogent `releases/latest` de ce dépôt ; à désactiver dans Réglages › À propos).

**Signature (facultative).** Sans secrets, les applis sont signées localement (« Ouvrir quand même »). Avec ces secrets du dépôt, la construction signe et notarise automatiquement :
- Mac (compte Apple Developer, 99 $/an) : `MAC_CERT_P12` (certificat « Developer ID Application » exporté en .p12, en base64), `MAC_CERT_PASSWORD`, `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD` (mot de passe d'app créé sur appleid.apple.com).
- Windows (certificat de signature de code) : `WIN_CERT_PFX` (.pfx en base64) et `WIN_CERT_PASSWORD`.

**Premier lancement d'une version non signée :**
- Mac : ouvrir le .dmg, glisser l'appli dans Applications, l'ouvrir une fois, puis Réglages Système › Confidentialité et sécurité › « Ouvrir quand même ».
- Windows : à l'écran bleu « Windows a protégé votre ordinateur », cliquer « Informations complémentaires » › « Exécuter quand même ».

**Spotify.** Sans compte ni clé : l'appli lit le lecteur intégrable de Spotify (`open.spotify.com/embed/…`) pour toute playlist publique ou album (jusqu'à 100 titres pour une playlist). Au-delà, pour une playlist privée ou les titres likés : export CSV avec Exportify, récupéré automatiquement dans Téléchargements.

## Sources de playlist

Spotify (lien public ou Exportify), Deezer (playlist publique), YouTube, SoundCloud, **Bandcamp** et CSV.

Bandcamp n'a pas d'API publique : `read_bandcamp_url` lit les données embarquées dans les pages (`data-tralbum` pour un album ou un morceau, `data-blob` pour une page fan) et l'API interne `fancollection/1/{wishlist,collection}_items` pour la wishlist et la collection, qui doivent être publiques. Chaque album est déplié en ses morceaux ; sur une compilation, « Artiste - Titre » est séparé. Le bouton Bandcamp d'un morceau venu de Bandcamp pointe directement sur sa page.

## Licence, crédits et utilisation

© Lutin. Logiciel libre sous licence **GNU AGPL v3** (voir `LICENSE`), fourni sans aucune garantie. L'appli livre **sockseek** de fiso64 (AGPL v3) sans modification ; le détail des composants et de leurs licences est dans [`CREDITS.md`](CREDITS.md) et dans l'appli (Réglages › À propos).

Playlist2Slsk télécharge des fichiers partagés par les utilisateurs du réseau Soulseek. Chaque utilisateur est seul responsable de ce qu'il télécharge et partage, et doit respecter le droit d'auteur applicable. Soutiens les artistes : le bouton Bandcamp mène à l'achat du morceau.

Spotify, Deezer, YouTube, SoundCloud, Bandcamp, Soulseek, rekordbox et Traktor sont des marques de leurs propriétaires. Ce projet n'est ni affilié à, ni approuvé par ces sociétés ; leurs noms indiquent seulement la compatibilité. Les playlists sont lues depuis les pages publiques de ces services, qui peuvent changer sans prévenir.

Un problème ou une idée : onglet *Issues* du dépôt, ou dans l'appli Réglages › À propos › *Signaler un problème*.
