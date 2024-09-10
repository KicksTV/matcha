import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    environment = os.getenv("MATCHA_ENVIRONMENT", "DEBUG")
    players_per_match = os.getenv("PLAYERS_PER_MATCH", 2)
    redis_options = {
        'host': os.getenv("REDIS_HOST", '172.23.109.48'),
        'port': os.getenv("REDIS_PORT", 6379),
        'username': os.getenv("REDIS_USERNAME", 'default'),
        'password': os.getenv("REDIS_PASSWORD", ''),
        'db': os.getenv("DB", 0),
    }
    redis_url = os.getenv("REDIS_URL", 'redis://172.23.109.48:6379/0')
    baseUrl = 'http://172.23.109.48:8000' if environment == 'DEBUG' else 'https://running-app-efd09e797a8a.herokuapp.com'
    
config = Config()