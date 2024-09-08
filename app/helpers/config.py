import os

class Config:
    environment = os.getenv("MATCHA_ENVIRONMENT", "debug")
    players_per_match = os.getenv("PLAYERS_PER_MATCH", 2)
    redis_options = {
        'host': os.getenv("REDIS_HOST", 'redis'),
        'port': os.getenv("REDIS_PORT", 6379),
        'password': os.getenv("REDIS_PASSWORD", ''),
    }
    redis_url = 'redis://redis:6379' # os.getenv("REDIS_URL", '172.23.109.48')
    baseUrl = 'http://172.23.109.48:8000' if environment == 'debug' else 'https://running-app-efd09e797a8a.herokuapp.com'

config = Config()