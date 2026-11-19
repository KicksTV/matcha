import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    environment = os.getenv("MATCHA_ENVIRONMENT", "DEBUG")

    # How many runners one match can hold. A pop is created as soon as
    # `min_players_per_match` compatible players are queued, and fills up to the max —
    # so 2 in the pool is still a 1v1, 6 are a 6-way race. Always parse to int: these
    # came back as strings from os.getenv and `players_per_match - 1` then raised
    # TypeError inside the worker's scan (matchmaking silently produced nothing).
    max_players_per_match = max(2, int(os.getenv("MATCH_MAX_PLAYERS", 6) or 6))
    min_players_per_match = max(2, min(
        int(os.getenv("MATCH_MIN_PLAYERS", 2) or 2),
        max_players_per_match,
    ))
    # Below this many players in the pool the rank window is ignored entirely: with a
    # thin queue, waiting for the perfect opponent means waiting forever.
    thin_pool_threshold = int(os.getenv("MATCH_THIN_POOL", 10) or 10)
    # How long a partial group is held back hoping to fill to max_players_per_match.
    # Only in effect while the pool has nothing else to offer that player; after this
    # many seconds the group pops with whoever it has (down to 2).
    fill_wait_seconds = max(0, int(os.getenv("MATCH_FILL_WAIT", 5) or 5))
    # Legacy alias (main_old.py, docs). New code uses the group's own size.
    players_per_match = max_players_per_match

    redis_options = {
        'host': os.getenv("REDIS_HOST", ''),
        'port': os.getenv("REDIS_PORT", 6379),
        'username': os.getenv("REDIS_USERNAME", ''),
        'password': os.getenv("REDIS_PASSWORD", ''),
        'db': os.getenv("DB", 0),
    }
    redis_url = os.getenv("REDIS_URL", '')
    baseUrl = os.getenv("BASE_URL", '')
    django_auth_token = os.getenv("DJANGO_AUTH_TOKEN", '') or os.environ['django_auth_token']
    
config = Config()