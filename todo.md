# TODO — Selfbot user

## A. Sécurité & bugs (faire en premier)

- [ ] **(action manuelle, à faire par toi)** Régénérer tous les secrets compromis (token Discord, clés exchangerate/Tatum/BlockCypher, identifiants proxy) — les anciens étaient en clair dans `main.py` / `config.ini`
- [x] Déplacer les secrets vers `.env` / variables d'environnement ; `.env.example` et `config.example.ini` créés
- [x] Retirer du code : token, clés API, proxy user/pass, IDs de canaux, adresse LTC, app_id fallback, user-agent figé, id de serveur ignoré (→ `IGNORED_GUILD_IDS`)
- [x] Supprimer l'affichage du token dans `settoken`
- [x] Remplacer `eval()` — par un évaluateur arithmétique AST (`literal_eval` ne sait pas calculer `2+3`)
- [x] Retirer tous les `ssl=False`
- [x] Compléter `requirements.txt` (+ `requirements-controller.txt`, `requirements-dev.txt`)
- [x] Définir / protéger `telegrambot`, `retryNitro`, `login_with_cookies`, `check_account`
- [x] `await` manquants : `get_ltc_transaction_detail`, `redeem_code`, `asyncio.sleep` (rbxgp), `response.json()` (inv/invs/invites)
- [x] `sendu` : détecte le dict d'erreur de `send_usdt_matic` (web3 déplacé dans un thread)
- [x] `on_ready` : fichiers absents tolérés ; plus de `os.remove`
- [x] `max_price` lu depuis la config ; `price_manager` démarré dans `on_ready`
- [x] `send_message` / `on_message_delete` : `get_channel()` non `None`
- [x] `on_command_error` + `create_task` suivis (`STATE.spawn`) + arrêt propre
- [x] Tests : helpers de parsing + config ; `compileall`

## B. Structure (sans changer le comportement)

- [x] `selfbot/config.py` — chargement/env/validation (dataclasses)
- [x] `selfbot/secrets.py` — chiffrement DPAPI / Fernet, store de tokens
- [x] `selfbot/logging_setup.py` — logs rotating, masquage des secrets
- [x] `selfbot/instance.py` — `InstanceState` (caches, tâches suivies, shutdown)
- [ ] Découper en cogs : sniper, tickets, nitro, crypto, roblox, invites, utility *(non fait : voir notes)*
- [x] `selfbot/services/` — sessions aiohttp partagées par API *(prêt ; `bot.py` ouvre encore une session par appel)*
- [x] `selfbot/storage/db.py` — SQLite à la place des `.txt` d'IDs et des réécritures de `config.ini`
- [ ] Remplacer les globals par `state` explicite partout *(partiel : config/secrets/tâches/DB migrés, ~100 globals de `bot.py` restent)*
- [x] `main.py` conservé comme simple wrapper (le code vit dans `selfbot/bot.py`)

## C. Multi-instance (contrôleur + workers isolés)

- [x] `selfbot/worker.py` — `python -m selfbot.worker --instance <id>`, heartbeat, arrêt gracieux
- [x] Contrôleur = **vrai bot applicatif** (`discord.py`, env séparé), `owner_ids` depuis l'env
- [x] Toutes les commandes contrôle protégées par `@commands.is_owner()` (+ vérification dans le service)
- [x] Commandes : `addtoken`, `start`, `stop`, `restart`, `list`, `status`, `remove`
- [x] Token masqué partout (`MTQ...xyz`), jamais loggué ; `addtoken` en DM uniquement
- [x] `selfbot/manager.py` — `Popen`, PID, logs par instance, restart avec backoff
- [x] Token passé par variable d'environnement enfant (jamais en ligne de commande)
- [x] Rate limiting par compte ; refus d'un token dupliqué ; audit des actions owner
- [x] Test : faux tokens / worker factice, crash d'une instance sans impact sur les autres

## Vérification finale

- [x] Recherche statique (test automatisé) : aucun secret, aucun `print(token)`, aucun `ssl=False`
- [x] Démarrage à froid sans secrets réels → échec explicite et lisible (code 2)
- [x] Deux instances tournent en parallèle avec des configs séparées (test avec faux worker)
- [ ] Test réel avec un vrai compte Discord *(non faisable ici)*
