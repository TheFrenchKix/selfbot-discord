# CLAUDE.md

Met à jour ce fichier à chaque modification du projet (commandes, modules, variables d'environnement, sécurité, état des tests).

## Projet

Selfbot Discord en Python (`C:\Users\lpecqueux\Documents\Mwa\selfbot-user`), dépôt `https://github.com/TheFrenchKix/selfbot-discord.git`, branche `main`. Utilisateur francophone : répondre en français, sans poser de questions inutiles, agir de façon autonome et tester avant de rendre la main.

Deux modes d'exécution :
- **Instance unique** : `python main.py` → `selfbot/bot.py` (bibliothèque `discord.py-self`, compte utilisateur).
- **Multi-instance** : contrôleur `python -m selfbot.controller` (vrai bot applicatif `discord.py`, token `CONTROLLER_TOKEN`) qui lance un worker isolé par compte (`selfbot/worker.py`, interpréteur `WORKER_PYTHON`). `discord.py` et `discord.py-self` sont incompatibles (même namespace `discord`) : deux environnements.

## Architecture (modules)

| Fichier | Rôle |
|---|---|
| `main.py` | Wrapper : `from selfbot.bot import run_bot` |
| `selfbot/bot.py` | Sélfbot principal : globals d'état, `SelfBot(commands.Bot)`, `on_message` (sniper de stock), commandes historiques (nitro, crypto, tickets, invites, vouch...), `run_bot()` |
| `selfbot/config.py` | `Settings` / `ApiKeys` / `Channels` / `Filters`, `load_settings`, `.env` (`load_dotenv`), `mask_token` |
| `selfbot/secrets.py` | Chiffrement (DPAPI sous Windows, Fernet via `SELFBOT_MASTER_KEY` ailleurs), `TokenStore` |
| `selfbot/logging_setup.py` | Logs rotatifs, `register_secret`, `redact` (masquage tokens/proxys) |
| `selfbot/storage/db.py` | SQLite (`id_sets`, `kv`, `audit`), import unique des anciens `.txt` |
| `selfbot/ui.py` | Mise en forme « façon embed » en Markdown (`card`, `ok/err/warn/info`, `list_card`, `chunk`) — un compte utilisateur ne peut pas envoyer d'embeds |
| `selfbot/helpers.py` | `calcul` (AST sûr, pas d'`eval`), extraction prix/URL/quantité, encodage de codes |
| `selfbot/instance.py` | `InstanceState` (settings, client, db, tâches suivies) |
| `selfbot/manager.py` | `InstanceManager` (start/stop/restart/rename, supervision, backoff, heartbeat, événements), `RateLimiter`, `FATAL_EXIT_CODE=2` |
| `selfbot/worker.py` | Process worker : `python -m selfbot.worker --instance <id>`, heartbeat, signaux |
| `selfbot/controller.py` | `ControllerService` (logique testable) + `build_bot()` (commandes `!`) + `main()` |
| `selfbot/webhook.py` | Notifications Discord des événements (embeds), URL chiffrée, validation du domaine Discord |
| `selfbot/shop.py` | Catalogue du shop Discord (HAR analysé : `shop/search` + `storefront/products/skus`), filtres, cache |
| `selfbot/shopwatch.py` | Diff de catalogue (nouveaux items, baisses de prix), comparaison d'items |
| `selfbot/tools.py` | Fonctions pures : durées, snowflakes, `SnipeStore`, `AfkState`, rappels, couleurs, dés, hash, base64 |
| `selfbot/extras.py` | Cog « Extras » : infos, snipe, AFK, rappels, notes, outils, modération |
| `selfbot/toolkit.py` | Fonctions pures : alias, `Stats`, crypto (adresses LTC, frais, alertes), traduction, sondages, validation d'export |
| `selfbot/cogbase.py` | Mixin `CogHelpers` (`out`, `fail`, kv SQLite, `_http_json`) |
| `selfbot/automation.py` | Cog « Automation » : pause, stats, export/import, price alerts, watch LTC, portfolio, gift info, alias/snippets, autoreact, tz, poll, outils de messages, `shop watch` |
| `selfbot/swap.py` | Fournisseurs de swap (Swapzone, FixedFloat HMAC, SideShift en cotation seule), devis, frais, `TrailingTrigger` |
| `selfbot/swapcog.py` | Cog « Swap » : `swapltc`, `swapusdt`, `swaprates`, `swapstatus`, `autosell`, `autobuy` |
| `litecoin.py` | Flux de prix LTC (websocket) |
| `tests/` | pytest, aucun appel à Discord (faux objets, serveur aiohttp local, `fake_worker.py`) |

Cogs chargés dans `SelfBot.setup_hook()` : `extras`, `automation`, `swapcog`. Les commandes de `bot.py` restent des `@client.command()`.

## Commandes principales (préfixe `.` par défaut)

- Sniper / stock : `auto`, `setauto`, `setmp`, `setdelay`, `stock`, `addstock(s)`, `addsnipe(s)`, `keyword`, `exclude`, `label`, `redeemer`…
- Nitro / crypto : `nb`, `checknb`, `savenb`, `ltc`, `crypto`, `bal`, `mybal`, `usdtbal`, `send`, `sendu`, `setprivateltc`, `setprivateusdt`, `resetprivate`, `lasttx`, `waittx`.
- Shop : `shop [type] [nom] [min= max= nitro= orbs= sort= limit= page= country= scan=]`, `shop watch|watches|unwatch|compare`.
- Extras : `ping`, `uptime`, `userinfo`, `avatar`, `banner`, `serverinfo`, `membercount`, `channelinfo`, `emojis`, `snowflake`, `snipe`, `editsnipe`, `firstmsg`, `afk`, `remind`, `reminders`, `unremind`, `note`, `roll`, `flip`, `choose`, `8ball`, `uuid`, `color`, `hash`, `b64`, `time`, `weather`, `define`, `qr`, `setstatus`, `kick`, `ban`, `unban`, `timeout`, `slowmode`.
- Automation : `pause`, `stats`, `export`, `import`, `gift info`, `price`, `price alert|alerts|del`, `fees`, `watch`, `portfolio`, `alias`, `snippet`, `react`, `autoreact`, `translate`, `tz`, `poll`, `copyid`, `jump`, `pins`, `purgeuser`.
- Swap : `swapltc <montant|all> [0x…] [force=yes]`, `swapusdt <montant|all> [ltc]`, `swaprates [montant] [ltc|usdt]`, `swapstatus <id> [token]`, `autosell <usd> <montant|all>`, `autobuy`, `stopautosell`, `stopautobuy`, `swapauto`.
- Aide : `howto [sujet]` (sujets : sniper, stock, tickets, crypto, nitro, invites, settings, shop, tools, automation, swap, utility).

Contrôleur (`!`, réservé aux `OWNER_IDS`) : `addtoken` (DM seulement), `start`, `stop`, `restart`, `remove`, `status`, `list`, `rename` (instance arrêtée), `logs <id> [n]`, `uptime`, `restartall confirm`, `stopall confirm`, `setwebhook` (DM seulement), `webhook`, `testwebhook`, `clearwebhook`.

## Variables d'environnement

Voir `.env.example` (modèle, sans valeurs). Priorité : env > `config.ini` (non secret) > défauts. Principales : `DISCORD_TOKEN`, `EXCHANGERATE_API_KEY`, `TATUM_API_KEY`, `BLOCKCYPHER_TOKEN`, `SWAPZONE_API_KEY`, `FIXEDFLOAT_API_KEY`, `FIXEDFLOAT_API_SECRET`, `SWAP_PROVIDER` (auto/swapzone/fixedfloat), `SWAP_MAX_FEE_PCT` (défaut 3), `LTC_ADDRESS`, `USDT_ADDRESS`, `MSG_CHANNEL_ID`, `VOUCH_CHANNEL_ID`, `TRANSACTION_CHANNEL_ID`, `CONTROLLER_TOKEN`, `OWNER_IDS`, `LOG_WEBHOOK_URL`, `WORKER_PYTHON`, `SELFBOT_MASTER_KEY`.

## Règles de sécurité (non négociables)

- Secrets uniquement dans `.env` ou variables d'environnement, jamais dans le code, `config.ini`, la ligne de commande ou les logs. Tokens toujours masqués (`MTQ...xyz`).
- `.env`, `config.ini`, `instances/`, `exports/`, `*.db`, `logs/`, `*.har`, `main_ancien.py` sont dans `.gitignore`. Ne jamais les committer.
- Les clés ne doivent jamais apparaître dans une réponse ou un commit. Si une clé est collée dans la conversation, la considérer comme exposée et demander de la régénérer.
- Pas d'automatisme abusif : pas de spam, DM de masse, raid, nuke, token grabber, ajout d'amis en masse. Refuser ces demandes.
- Swap : confirmation par mot de passe dans le salon de transactions, frais max vs marché (`SWAP_MAX_FEE_PCT`), validation de l'adresse de destination ET de l'adresse de dépôt renvoyée par l'API avant envoi, un seul swap à la fois, pas de relance automatique après échec (les fonds peuvent être partis).
- Webhook : URL chiffrée, seules les URL `https://discord.com/api/webhooks/...` sont acceptées.
- Clés privées LTC/USDT : jamais affichées ; `resetprivate` les efface.
- Hors Windows, `SELFBOT_MASTER_KEY` est obligatoire pour chiffrer les tokens.

## Décisions et points connus

- Les embeds sont simulés en Markdown (`selfbot/ui.py`) : un compte utilisateur ne peut pas en envoyer. Le webhook du contrôleur, lui, envoie de vrais embeds.
- `empty keywords` : les mots-clés vides du vieux `config.ini` sont ignorés (avant, la chaîne vide correspondait à tout).
- Stockage : SQLite remplace les fichiers `.txt` (importés une fois, non modifiés) et les réécritures de `config.ini`.
- Le shop Discord ne permet pas de filtrer par nom côté serveur : filtrage fait localement sur les SKU récupérés (`scan=`, 400 max).
- L'achat au shop n'est PAS implémenté : il faut un HAR d'un achat réel (endpoints de paiement inconnus). Proposition : `.buy` avec fiche, confirmation par code, plafond `SHOP_MAX_PRICE`.
- `shop owned` non implémenté (endpoint des items possédés absent du HAR).
- `checknb` supprime puis réédite le même message (bug existant, non corrigé).
- Dépôt git : historique `first commit` puis `feat: Webhook for logs`, puis `feat: shop, utility commands, automation, swap and controller additions` (99cb968).

## Tests et vérifications

- Lancer : `py -3 -m pytest` (dépendances : `requirements-dev.txt`). État : 140 tests passent.
- `pyflakes` propre sur les nouveaux modules.
- Vérifications réelles faites : API shop (HAR), CoinGecko, BlockCypher (frais), SideShift, Swapzone et FixedFloat en cotation seule (aucun ordre créé), Google Translate, Binance (référence de prix).
- Non testé sur un vrai compte Discord : toutes les commandes de cog sont testées avec de faux objets uniquement.

## Environnement

- Windows 11, Git Bash et PowerShell. Python : `py -3` (3.13, avec pytest). Environnement `discord.py` pour le contrôleur : `AppData\Local\Temp\venv2` (non versionné).
- Scratchpad temporaire : `C:\Users\LPECQU~1\AppData\Local\Temp\claude\...\scratchpad`. Ne pas y mettre de fichiers à conserver.
