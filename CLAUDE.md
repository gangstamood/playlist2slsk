# playlist2slsk — contexte projet

Outil perso d'Alan : passer d'une playlist Spotify à des fichiers audio propres téléchargés via Soulseek.
Répondre en français, ton direct.

## Ce que fait l'outil

`playlist2slsk.py` est un wrapper Python autour de **sockseek** (anciennement sldl / slsk-batchdl,
https://github.com/fiso64/sockseek, binaire .NET autonome).

1. Il lit la source : un export CSV de playlist (Exportify, TuneMyMusic, Soundiiz) avec détection automatique des colonnes, ou une URL Spotify via l'API (client credentials).
2. Il écrit un CSV de travail `Artist,Title,Album,Length,URI` (artiste principal seulement, pour de meilleures recherches).
3. Il lance `sockseek <csv> -o <staging> --name-format "{snum}" --no-write-index --input-type csv`.
   `{snum}` est le numéro de ligne (1-indexé), donc chaque fichier téléchargé (`1.flac`, `2.mp3`…) est rattaché sans ambiguïté à son morceau.
4. Post-traitement :
   - déplacement et renommage selon `--pattern` (défaut `{artist} - {title}`) ;
   - réécriture des tags avec mutagen (MP3/ID3, FLAC, MP4/M4A, OGG/Opus, WAV/AIFF) ;
   - intégration de la jaquette, cherchée dans l'ordre : URL du CSV ou de l'API (upgrade i.scdn.co en 640 px `ab67616d0000b273`), puis oEmbed Spotify public `open.spotify.com/oembed`, puis iTunes Search en 1000 px.
5. Il écrit `<playlist>.m3u8` dans l'ordre d'origine et `missing.csv` (morceaux non trouvés, au même format qu'un export, donc réutilisable).
6. Reprise : un morceau dont le fichier final existe déjà est ignoré à la relance.

Les arguments après `--` sont passés tels quels à sockseek (`--strict-title`, `--length-tol 10`, `--desperate`…).
Les identifiants Soulseek restent dans `~/.config/sockseek/sockseek.conf` et le script ne les manipule jamais.

## Points sockseek utiles (vérifiés dans le code source)

- Détection auto des colonnes CSV : artist/artistname/artists, title/song/trackname/track, album/albumname, length/duration (en secondes par défaut, `--time-format` sinon), uri/url/id.
- Variables de `--name-format` : `{snum}` (n° d'item), `{row}`/`{line}`, `{sartist}`, `{stitle}`, `{salbum}`, `{uri}`, plus les tags du fichier (`{artist}`, `{title}`…).
- Index `_index.csv` désactivé ici (`--no-write-index`) : c'est le wrapper qui gère la reprise.
- Limite Soulseek par défaut : 34 recherches / 220 s.
- Spotify par URL : sockseek exige une app développeur dont le propriétaire a Spotify Premium. Le « Development Mode » bloque souvent la lecture des playlists, d'où le choix du CSV par défaut.

## État

- Testé dans le cloud avec un faux sockseek et de vrais fichiers audio générés par ffmpeg : renommage, caractères interdits, tags + jaquette dans les 4 formats, m3u, missing.csv, reprise. Tout est OK.
- **Pas encore testé** : le vrai téléchargement Soulseek, la récupération des jaquettes en ligne, la lecture d'une URL Spotify.
- Prochaine étape : premier vrai test sur Mac avec `--dry-run`, puis `-n 5`.

## Idées notées

- Brancher sur le projet perso « trackid » (identification de morceaux dans des sets underground) : identifier, puis télécharger directement.
- Interface (Streamlit ?) pour coller un lien ou déposer un CSV.

## Commandes

```bash
pip3 install -r requirements.txt
python3 playlist2slsk.py ~/Downloads/playlist.csv --dry-run
python3 playlist2slsk.py ~/Downloads/playlist.csv --lossless -n 5
```
