# Selfbot user

Selfbot Discord (compte utilisateur) : sniper de stocks, tickets, codes Nitro, outils crypto (LTC / USDT Polygon),
invitations et utilitaires. Il peut tourner seul (une instance) ou être piloté par un **contrôleur** qui gère
plusieurs comptes dans des processus isolés.

> ⚠️ Automatiser un compte utilisateur est contraire aux conditions d'utilisation de Discord : le compte peut être
> suspendu. À utiliser à vos risques, avec vos propres comptes.

## Sommaire
1. [Prérequis](#1-prérequis)
2. [Installation](#2-installation)
3. [Configuration](#3-configuration)
4. [Mode instance unique](#4-mode-instance-unique)
5. [Mode multi-instance](#5-mode-multi-instance-contrôleur)
6. [Commandes du selfbot](#6-commandes-du-selfbot)
7. [Sécurité](#7-sécurité)
8. [Tests](#8-tests)
9. [Structure du projet](#9-structure-du-projet)
10. [Dépannage](#10-dépannage)

---

## 1. Prérequis

- Python 3.10 ou plus (testé en 3.12)
- Un token de compte Discord (le vôtre)
- Optionnel : clés API exchangerate / Tatum / BlockCypher, un proxy
- Pour le mode multi-instance : un **bot applicatif** Discord (token créé sur le portail développeur)

## 2. Installation

```powershell
git clone <repo> selfbot-user
cd selfbot-user
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS : source .venv/bin/activate
pip install -r requirements.txt
```

Pour le contrôleur, `discord.py` (bot applicatif) est **incompatible** avec `discord.py-self` (même nom de
module `discord`). Utilisez un second environnement :

```powershell
python -m venv .venv-controller
.venv-controller\Scripts\activate
pip install -r requirements-controller.txt
```

## 3. Configuration

Les secrets vivent **uniquement** dans `.env` (ignoré par git). Les réglages non secrets sont dans `config.ini`.

```powershell
copy .env.example .env
copy config.example.ini config.ini
```

### `.env` (variables)

| Variable | Rôle |
|---|---|
| `DISCORD_TOKEN` | **Requis** (instance unique). Token du compte |
| `EXCHANGERATE_API_KEY` | Conversion de devises |
| `TATUM_API_KEY` | Envoi de LTC |
| `BLOCKCYPHER_TOKEN` | Détail des transactions LTC (`tltc`) |
| `PROXY_URL` | Proxy optionnel (`http://user:pass@host:port/`) |
| `USER_AGENT` | Optionnel ; par défaut celui de la bibliothèque Discord |
| `COMMAND_PREFIX` | Préfixe des commandes (défaut `.`) |
| `SWAPZONE_API_KEY`, `FIXEDFLOAT_API_KEY`, `FIXEDFLOAT_API_SECRET` | Fournisseurs de swap (au moins un pour exécuter ; `swaprates` marche sans clé) |
| `SWAP_PROVIDER`, `SWAP_MAX_FEE_PCT` | `auto` (meilleur devis) / `swapzone` / `fixedfloat` ; frais max acceptés vs prix du marché (défaut 3 %) |
| `LTC_ADDRESS`, `USDT_ADDRESS` | Vos adresses de réception/envoi |
| `MSG_CHANNEL_ID`, `VOUCH_CHANNEL_ID`, `TRANSACTION_CHANNEL_ID` | Salons de notification, vouch, transactions |
| `IGNORED_GUILD_IDS` | Serveurs ignorés par le sniper (ids séparés par des virgules) |
| `MAX_PRICE` | Prix max de l'auto-achat |
| `KEYWORDS`, `EXCLUDE_KEYWORDS`, `LABELS_CHECK` | Remplacent `config.ini` si définis |
| `DEBUG` | `1` pour les logs de debug |
| `CONTROLLER_TOKEN`, `OWNER_IDS` | Contrôleur : token du bot et ids des propriétaires |
| `INSTANCES_DIR` | Dossier des instances (défaut `instances`) |
| `WORKER_PYTHON` | Interpréteur Python des workers (voir §5) |
| `SELFBOT_MASTER_KEY` | Clé Fernet (hors Windows), `python -m selfbot.secrets genkey` |

Priorité : variables d'environnement > `config.ini` > valeurs par défaut.

### `config.ini`

```ini
[settings]
keywords = nitro, robux
excludekeywords = scam, fake
labelscheck = buy, ticket, open

[Filters]
max_price = 100
```

Les commandes `keyword`, `exclude`, `label` (et leurs `rm*`) modifient ces listes ; elles sont alors
enregistrées dans la base SQLite `selfbot.db`, plus dans `config.ini`.

## 4. Mode instance unique

```powershell
python main.py
```

- Sans `DISCORD_TOKEN`, le programme s'arrête avec un message explicite (code 2).
- Un token invalide ou révoqué donne aussi un message clair et un code de sortie 2.
- Données locales : `selfbot.db` (ensembles d'ids, mots-clés), `logs/selfbot.log` (rotation automatique, secrets masqués).
- Ctrl+C arrête proprement les tâches de fond.

Les anciens `stocks.txt` / `tickets.txt` sont importés une fois dans la base puis laissés intacts.

## 5. Mode multi-instance (contrôleur)

Un bot applicatif pilote N comptes ; chaque compte tourne dans **son propre processus**, avec son dossier
`instances/<id>/` (cwd, `config.ini`, `.env`, logs et base séparés). Le crash d'une instance n'affecte pas les autres.

### Démarrage

Dans `.env` du contrôleur :

```
CONTROLLER_TOKEN=<token du bot applicatif>
OWNER_IDS=123456789012345678,987654321098765432
WORKER_PYTHON=C:\chemin\vers\selfbot-user\.venv\Scripts\python.exe
```

```powershell
.venv-controller\Scripts\activate
python -m selfbot.controller
```

Invitez le bot sur un serveur ou parlez-lui en message privé. Le préfixe est `!` (`CONTROLLER_PREFIX` pour le changer).

### Commandes (réservées aux `OWNER_IDS`)

| Commande | Effet |
|---|---|
| `!addtoken <id> <token>` | Ajoute un compte (**en message privé uniquement**, le message est supprimé) |
| `!start <id>` | Démarre l'instance |
| `!stop <id>` | Arrête proprement l'instance |
| `!restart <id>` | Redémarre |
| `!status <id>` | État, pid, redémarrages, âge du heartbeat |
| `!list` | Liste toutes les instances |
| `!remove <id>` | Arrête et supprime l'instance et son token |
| `!rename <ancien> <nouveau>` | Renomme une instance **arrêtée** (token + dossier de données) |
| `!logs <id> [lignes]` | Dernières lignes du log de l'instance (secrets masqués, 50 max) |
| `!uptime` | Durée de fonctionnement du contrôleur et de chaque instance |
| `!restartall confirm` | Redémarre toutes les instances actives |
| `!stopall confirm` | Arrête toutes les instances |
| `!setwebhook <url>` | Définit le webhook de logs (**en message privé uniquement**, le message est supprimé) |
| `!webhook` | Affiche le webhook configuré (masqué) |
| `!testwebhook` | Envoie un message de test |
| `!clearwebhook` | Supprime le webhook |

`<id>` : 1 à 32 caractères (lettres, chiffres, `_`, `-`).

### Logs des instances par webhook

Créez un webhook dans un salon (Paramètres du salon → Intégrations → Webhooks), puis, en message privé au
contrôleur : `!setwebhook https://discord.com/api/webhooks/<id>/<token>`. Vous pouvez aussi définir
`LOG_WEBHOOK_URL` dans `.env` (utilisée seulement si aucun webhook n'est déjà enregistré).

Un embed est envoyé pour chaque événement : instance ajoutée, supprimée, **démarrée**, **redémarrée**,
**arrêtée**, **relancée après un crash**, **crash** (code de sortie, uptime, délai avant relance et dernières lignes
de log), **erreur fatale** (token invalide, non relancée) et **heartbeat perdu**. Les secrets sont masqués dans les
extraits de log.

L'URL du webhook est un secret : elle est stockée chiffrée (`instances/webhook.json`), validée (domaine Discord
uniquement) et toujours affichée masquée. Les envois sont asynchrones, relancés en cas de 429 / erreur réseau, et
n'ont jamais d'effet sur la supervision.

### Réglages propres à une instance

Créez `instances/<id>/instance.json` avec des variables non secrètes :

```json
{ "env": { "MSG_CHANNEL_ID": "111", "MAX_PRICE": "3", "PROXY_URL": "http://host:port/" } }
```

`DISCORD_TOKEN`, `CONTROLLER_TOKEN` et `OWNER_IDS` ne peuvent pas y être redéfinis. Vous pouvez aussi déposer un
`config.ini` ou un `.env` dans le dossier de l'instance.

### Fonctionnement

- Tokens stockés **chiffrés** dans `instances/tokens.json` (DPAPI sous Windows, Fernet ailleurs), passés au worker
  par variable d'environnement, jamais en ligne de commande, et toujours affichés masqués (`MTQ...xyz`).
- Redémarrage automatique en cas de crash, avec backoff exponentiel. Un worker au heartbeat figé (> 90 s) est relancé.
  Un token invalide (code 2) n'est pas relancé en boucle.
- Limite de démarrages par compte, refus des tokens en double, journal d'audit des actions owner (base `instances/manager.db`).
- Logs : `instances/<id>/logs/` et `logs/controller.log`.

Lancer un worker à la main (debug) : `python -m selfbot.worker --instance <id>` avec `DISCORD_TOKEN` défini.

## 6. Commandes du selfbot

Préfixe `.` par défaut. `.howto` affiche l'aide intégrée (`.howto commands`, `sniper`, `stock`, `crypto`, `nitro`…).
Les commandes sont tapées depuis votre propre compte.

| Domaine | Commandes |
|---|---|
| Aide / debug | `howto`, `status`, `debugmode`, `proxytest`, `proxyinfo`, `getappid`, `debugappid`, `debugmsg`, `checkmsg` |
| Réglages | `setdelay`, `setauto`, `setmp`, `settc`, `setmsg`, `setvouch`, `settoken`, `activity`, `sleep`, `nomove`, `update`, `sync` |
| Sniper / stock | `auto`, `redeemer`, `stock`, `stocks`, `snipes`, `addstock(s)`, `addsnipe(s)`, `rmstock`, `rmsnipe`, `rmall`, `rasnipe`, `addcst`, `addcsn`, `clean`, `sclean`, `tclean`, `deleted`, `st`, `addst`, `uploadst`, `uploadsn` |
| Tickets | `tickets`, `addticket(s)`, `rmticket`, `tkt`, `gettkt`, `getcat`, `refreshcache`, `manualcache`, `allcachestatus` |
| Mots-clés | `keyword`, `getkw`, `rmkw`, `exclude`, `getex`, `rmex`, `label`, `getlb`, `rmlb` |
| Nitro | `nb`, `nbs`, `checknb`, `checknbs`, `savenb`, `saveacc`, `vnb`, `vsb`, `vrb` |
| Crypto | `ltc`, `eur`, `usd`, `cv`, `cvt`, `crypto`, `croix`, `bal`, `mybal`, `usdtbal`, `addy`, `addyu`, `setaddy`, `setaddyu`, `tltc`, `alltx`, `lasttx`, `waittx`, `stopwaittx` |
| Envoi de fonds | `setprivateltc`, `setprivateusdt`, `resetprivate`, `settatum`, `send`, `sendu` |
| Serveurs / invitations | `createinvites`, `joinfromfile`, `inv`, `invs`, `invites`, `guilds`, `nuke` |
| Shop Discord | `shop` (décorations, effets, plaques, cadres, bundles ; filtres nom/prix/Nitro/orbs, tri, pagination : `.shop help`) |
| Infos | `ping`, `uptime`, `userinfo`, `avatar`, `banner`, `serverinfo`, `membercount`, `channelinfo`, `emojis`, `snowflake` |
| Messages | `snipe`, `editsnipe`, `firstmsg`, `afk`, `remind`, `reminders`, `unremind`, `note` |
| Outils | `roll`, `flip`, `choose`, `8ball`, `uuid`, `color`, `hash`, `b64`, `qr`, `time`, `weather`, `define`, `setstatus` |
| Modération | `kick`, `ban`, `unban`, `timeout`, `slowmode` (permissions du serveur requises, actions auditées) |
| Contrôle du sniper | `pause [1h\|off\|status]`, `stats [reset]`, `export`, `import [fichier]` (config sans secrets, dans `exports/`) |
| Shop (suite) | `shop watch <filtres>` (nouveaux items / baisses de prix), `shop watches`, `shop unwatch <n>`, `shop compare a \| b` |
| Crypto (suite) | `price <sym>`, `price alert\|alerts\|del`, `fees`, `watch <adresse LTC>`, `portfolio`, `gift info <code>` (sans valider le cadeau) |
| Confort | `alias`, `snippet`, `react`, `autoreact`, `translate`, `tz`, `poll`, `copyid`, `jump`, `pins`, `purgeuser` |
| Swap LTC ⇄ USDT | `swapltc`, `swapusdt`, `swaprates`, `swapstatus`, `autosell`, `autobuy`, `stopautosell`, `stopautobuy`, `swapauto` (`.howto swap`) |
| Divers | `math`, `dm`, `spam`, `ppl`, `vouch`, `vpc`, `vpsc`, `weed`, `stamp`, `bingo`, `rbxgp` |

### Swap LTC ⇄ USDT (Polygon)

Commandes reprises de l'ancien bot (`main_ancien.py`) : `swapltc <montant|all> [adresse 0x…]`, `swapusdt <montant|all> [adresse LTC]`,
`swapstatus`, `autosell <prix USD> <montant|all>` et `autobuy` (+ `stopautosell`, `stopautobuy`, `swapauto`).
Il faut les clés privées (`setprivateltc`, `setprivateusdt`) et, pour exécuter, une clé API Swapzone ou FixedFloat dans `.env`.

Garde-fous ajoutés : confirmation par mot de passe dans le salon de transactions, refus si le coût dépasse `SWAP_MAX_FEE_PCT`
par rapport au prix du marché (`force=yes` pour passer outre), adresse de destination **et** adresse de dépôt renvoyée par l'API
validées avant tout envoi, un seul swap à la fois, journal d'audit, et les automatismes s'arrêtent après le premier déclenchement.

`swaprates [montant] [ltc|usdt]` compare les devis et leur coût réel sous le prix du marché, sans clé API (SideShift en lecture seule
+ vos fournisseurs configurés). Faites-le avant un gros swap : les frais varient beaucoup d'un service à l'autre.

### Envoi de crypto (`send`, `sendu`)

1. `.setprivateltc <clé> <mot de passe>` ou `.setprivateusdt <clé> <mot de passe>` (stockés **en mémoire seulement**, perdus à l'arrêt).
2. `.send <adresse> <montant>[€/$]` ou `.sendu <adresse> <montant|all>`.
3. Le bot demande de retaper le mot de passe dans le salon de transactions dans les 30 s.
4. `.resetprivate` efface les clés de la mémoire.

`settatum` et `settoken` ne persistent rien : mettez la valeur dans `.env` pour la conserver.

## 7. Sécurité

- **Ne commitez jamais** `.env`, `config.ini`, `instances/`, `*.db`, `logs/` (déjà dans `.gitignore`).
- Si un secret a déjà été exposé (ancienne version du code), **régénérez-le** : token Discord, clés API, identifiants du proxy.
- Les logs masquent tokens, `user:pass@` des proxys et en-têtes `authorization`.
- Toutes les vérifications TLS sont actives (plus de `ssl=False`).
- Le contrôleur ignore silencieusement les non-owners.

## 8. Tests

```powershell
pip install -r requirements-dev.txt
pytest
```

Couvre : configuration, parsing de messages, calcul sûr, chiffrement des secrets, masquage des logs, SQLite, manager
(faux worker : crash isolé, backoff, rate limit, arrêt gracieux, deux instances en parallèle), droits owner du
contrôleur, absence de secrets dans le code et démarrage à froid sans configuration. Aucun test ne contacte Discord.

## 9. Structure du projet

```
main.py                  point d'entrée (wrapper)
litecoin.py              flux de prix LTC (websocket Binance)
selfbot/
  bot.py                 le selfbot (commandes, événements)
  config.py              chargement/validation de la configuration
  secrets.py             chiffrement DPAPI/Fernet, store de tokens
  logging_setup.py       logs rotatifs avec masquage des secrets
  instance.py            état d'instance (tâches suivies, arrêt propre)
  ui.py                  mise en forme « façon embed » (cartes, bandeaux, listes)
  helpers.py             fonctions pures (parsing, calcul sûr, fichiers d'ids)
  storage/db.py          SQLite (ensembles d'ids, réglages, audit)
  worker.py              processus d'une instance (heartbeat, arrêt gracieux)
  manager.py             supervision des workers (backoff, rate limit, audit)
  controller.py          bot applicatif de contrôle
  webhook.py             notifications des instances vers un webhook Discord
tests/                   suite pytest
```

## 10. Dépannage

| Symptôme | Cause / solution |
|---|---|
| `Configuration invalide: DISCORD_TOKEN est absent` | Renseignez `.env` (voir §3) |
| `Token invalide ou révoqué` | Régénérez le token ; le manager ne relance pas ce cas |
| Le contrôleur plante sur `import discord` | Il doit tourner dans l'environnement `discord.py`, pas `discord.py-self` |
| Les workers ne démarrent pas | Vérifiez `WORKER_PYTHON` (venv avec `discord.py-self`) et `instances/<id>/logs/worker.out.log` |
| `Trop de démarrages pour <id>` | Limite par compte (5 / 10 min) ; patientez |
| Le sniper ne réagit à rien | Vérifiez `keywords`, `max_price`, les salons ajoutés (`addstock`) et `.auto` |
| `Application_id introuvable` | Le message n'a pas d'interaction de bot : cache de boutons à rafraîchir (`refreshcache`) |
| Sous Linux : erreur de secrets | Définissez `SELFBOT_MASTER_KEY` (DPAPI n'existe que sous Windows) |
