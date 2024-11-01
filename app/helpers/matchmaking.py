import asyncio
import json
import logging
import time

from redis import asyncio as aioredis, Redis

from app.helpers.config import config

logger = logging.getLogger("matcha")

# Redis  is used for matchmaking pool
# Redis /2 is used to put matches found

r = Redis(**config.redis_options)

async def _put_user_queue(user_id: str, user_ordinal: float, t: float = time.time()):
    redis = await aioredis.from_url(f"{config.redis_url}")
    # logger.debug(f"adding rank to queue: {user_ordinal}")
    await redis.zadd("matchmaking_pool", {user_id: user_ordinal})
    await redis.zadd("matchmaking_time", {user_id: t})
    await redis.aclose()

async def _remove_user_queue(user_id: str):
    redis = await aioredis.from_url(f"{config.redis_url}")
    # logger.debug(f"adding rank to queue: {user_ordinal}")
    await redis.zrem("matchmaking_time", user_id)
    await redis.zrem("matchmaking_pool", user_id)
    await redis.aclose()

async def _get_match(channel: aioredis.client.PubSub, user_id: str):
    while True:
        msg = await channel.get_message(ignore_subscribe_messages=True)
        if msg is not None:
            logger.debug(f"(Reader) Message Received: {msg}")
            data = json.loads(msg["data"].decode())
            logger.debug(f"Checking user with id: {user_id}")
            if user_id in data["players"]:
                logger.debug(f"User with id is in this match: {user_id}")
                match = data
                break
    return match

async def _get_match_proceeding(channel: aioredis.client.PubSub, user_id: str):
    while True:
        msg = await channel.get_message(ignore_subscribe_messages=True)
        if msg is not None:
            logger.debug(f"(Reader) Message Received: {msg}")
            match = json.loads(msg["data"].decode())
            logger.debug(f"Checking user with id: {user_id}")
            logger.debug(user_id in match["players"])

            if 'timedout' in match and match['timedout']:
                logger.debug(f"Not all players responded in time. Match cancelled!")
                match['proceeding'] = False
                break
            elif len(list(filter(lambda r: r == "ACCEPTED", match["responses"]))) >= config.players_per_match:
                logger.debug(f"All players have ACCEPTED the queue pop.")
                match['proceeding'] = True
                break
            elif len(match["responses"]) == config.players_per_match:
                logger.debug(f"All players have responded BUT not all accepted. Match cancelled!")
                match['proceeding'] = False
                break
    return match

async def _listen_match_channel(channel: aioredis.client.PubSub):
    while True:
        msg = await channel.get_message(ignore_subscribe_messages=True)
        if msg is not None:
            logger.debug(f"(Reader) Message Received: {msg}")
            data = json.loads(msg["data"].decode())
            match = data
            break
    return match


async def search_match(user: dict) -> dict:
    # Add user to the queue
    await _put_user_queue(user["id"], user["ordinal"])
    # Wait for match to be found
    r = aioredis.from_url(f"{config.redis_url}")
    async with r.pubsub() as pubsub:
        await pubsub.subscribe("matches")
        match_task = asyncio.create_task(_get_match(pubsub, user["id"]))
        await match_task
        match = match_task.result()
    return match

async def match_responses(match: dict, user_id: str):
    r = aioredis.from_url(f"{config.redis_url}")
    async with r.pubsub() as pubsub:
        await pubsub.subscribe("match_responses")
        match_task = asyncio.create_task(_get_match_proceeding(pubsub, user_id))
        await match_task
        match = match_task.result()
    return match

async def start_match(match: dict):
    r = aioredis.from_url(f"{config.redis_url}")
    await r.publish("match_responses", json.dumps(match))
    return match

async def match_result(match: dict):
    r = aioredis.from_url(f"{config.redis_url}")
    async with r.pubsub() as pubsub:
        await pubsub.subscribe(f"match_result:{match['id']}")
        match_task = asyncio.create_task(_listen_match_channel(pubsub))
        await match_task
        match = match_task.result()
    return match

async def match_finished(match: dict):
    r = aioredis.from_url(f"{config.redis_url}")
    async with r.pubsub() as pubsub:
        await pubsub.subscribe(f"match_finished:{match['id']}")
        match_task = asyncio.create_task(_listen_match_channel(pubsub))
        await match_task
        match = match_task.result()
    return match