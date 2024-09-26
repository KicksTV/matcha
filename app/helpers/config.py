import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    environment = os.getenv("MATCHA_ENVIRONMENT", "DEBUG")
    players_per_match = os.getenv("PLAYERS_PER_MATCH", 2)
    redis_options = {
        'host': os.getenv("REDIS_HOST", ''),
        'port': os.getenv("REDIS_PORT", 6379),
        'username': os.getenv("REDIS_USERNAME", ''),
        'password': os.getenv("REDIS_PASSWORD", ''),
        'db': os.getenv("DB", 0),
    }
    redis_url = os.getenv("REDIS_URL", '')
    baseUrl = os.getenv("BASE_URL", '')
    
config = Config()