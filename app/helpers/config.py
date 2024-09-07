import os

class Config:
    environment = os.getenv("MATCHA_ENVIRONMENT", "debug")
    players_per_match = os.getenv("PLAYERS_PER_MATCH", 2)
    redis_host = os.getenv("REDIS_HOST", '172.23.109.48')

config = Config()