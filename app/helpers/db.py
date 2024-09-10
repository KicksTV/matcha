import logging
import json
import time

from redis import asyncio as aioredis, Redis
from redis.commands.search.query import Query

from app.helpers.config import config

logger = logging.getLogger("matcha")

r = Redis(**config.redis_options)

# sync funcs

def get_all_users() -> dict:
    results = r.ft().search(Query("*"))
    return results.docs
    
# async funcs

async def update_ordinal(user_id: str, ordinal: float):
    conn = await aioredis.from_url(f"{config.redis_url}", encoding="utf-8")
    await conn.json().set(f'user:{user_id}', ".ordinal", ordinal)
    await conn.aclose()

async def update_games_played(user_id: str):
    conn = await aioredis.from_url(f"{config.redis_url}", encoding="utf-8")
    await conn.json().numincrby(f'user:{user_id}', ".games_played", 1)
    await conn.aclose()

async def add_user(userDict: dict) -> dict:
    conn = await aioredis.from_url(f"{config.redis_url}", encoding="utf-8")
    user_id = str(userDict['id'])
    user = {
        "id": user_id,
        "username": userDict['username'],
        "firstName": userDict['player']['firstName'],
        "lastName": userDict['player']['lastName'],
        "gender": userDict['player']['gender'],
        "rank": userDict['player']['rank'],
        "ordinal": userDict['player']['ordinal'],
        "queueStart": time.time()
    }
    logger.debug(f"Adding {user} to database")
    await conn.json().set(f'user:{user_id}', "$", user)
    await conn.aclose()
    return user

async def del_user(user_id: str):
    conn = await aioredis.from_url(f"{config.redis_url}", encoding="utf-8")
    user = await conn.json().delete(f'user:{user_id}')
    await conn.aclose()

async def get_user(user_id: str) -> dict:
    conn = await aioredis.from_url(f"{config.redis_url}", encoding="utf-8")
    user = await conn.json().get(f'user:{user_id}')
    await conn.aclose()
    if user:
        return user
    else:
        return None
    
async def add_match(match: dict):
    conn = await aioredis.from_url(f"{config.redis_url}", encoding="utf-8")
    # set the time when queue popped
    match.update({
        'status': 'STARTING',
        'proceeding': False,
        'responses': ['' for i in range(len(match['players']))],
    })
    logger.debug(f"Adding match {match} to database.")
    match_id = match["id"]
    await conn.json().set(f'match:{match_id}', "$", match)
    await conn.aclose()
    return match

async def get_match(match_id: str):
    conn = await aioredis.from_url(f"{config.redis_url}", encoding="utf-8")
    match = await conn.json().get(f'match:{match_id}')
    await conn.aclose()
    if match:
        return match
    else:
        return None

async def insert_match_response(match_id: str, user_id: str, response: str, index: int):
    conn = await aioredis.from_url(f"{config.redis_url}", encoding="utf-8")
    await conn.json().arrinsert(f'match:{match_id}', "$.responses", index, response)
    await conn.aclose()

async def update_match_response(match_id: str, user_id: str, response: str):
    conn = await aioredis.from_url(f"{config.redis_url}", encoding="utf-8")
    await conn.json().arrappend(f'match:{match_id}', "$.responses", response)
    await conn.aclose()

async def update_match_proceeding(match_id: str, proceeding: bool):
    conn = await aioredis.from_url(f"{config.redis_url}", encoding="utf-8")
    await conn.json().set(f'match:{match_id}', "$.proceeding", proceeding)
    await conn.aclose()

async def get_match_responses(match_id: str):
    conn = await aioredis.from_url(f"{config.redis_url}", encoding="utf-8")
    match = await conn.json().get(f'match:{match_id}')
    await conn.aclose()
    if match:
        return match
    else:
        return None