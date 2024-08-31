import os

class Config:
    environment = os.getenv("MATCHA_ENVIRONMENT", "debug")
    players_per_match = os.getenv("PLAYERS_PER_MATCH", 2)

config = Config()