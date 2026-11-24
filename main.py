"""Point d'entrée historique: simple wrapper autour de ``selfbot.bot``.

    python main.py                       # instance unique (DISCORD_TOKEN dans .env)
    python -m selfbot.worker --instance <id>   # instance gérée par le contrôleur
    python -m selfbot.controller         # contrôleur multi-instance (bot applicatif)
"""
from selfbot.bot import run_bot

if __name__ == "__main__":
    run_bot()
