import asyncio
import json
import logging
import requests
import time
import signal
import secrets
import sys
import redis

from threading import Timer

from app.helpers.db import add_match
from app.helpers.config import config

logger = logging.getLogger("matcha_worker")
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
logger.addHandler(ch)

# Redis /1 is used for matchmaking pool
# Redis /2 is used to put matches found

matchmaking_pool = redis.Redis(host="redis", port=6379, db=1)
pub = redis.Redis(host="redis", port=6379, db=2)

baseUrl = 'http://192.168.0.38:8000' if config.environment == 'debug' else 'https://running-app-efd09e797a8a.herokuapp.com'


def publish_match(player_ids):
    logger.debug("Publishing match...")

    # create match sql record
    match_data = {
        'players': [pid.decode('utf-8') for pid in player_ids],
        'status': 'STARTING',
        'createdOn': time.time()
    }
    resp = requests.post(f'{baseUrl}/api/matches/init-match/', data=json.dumps(match_data), headers={'Content-Type': 'application/json'})
    if resp.ok:
        match_data = resp.json()
        match = asyncio.run(add_match(match_data))
        logger.debug(f"Created match: {match}")
        pub.publish("queue", json.dumps(match_data))

        logger.debug(f"Starting queue pop timer")
        # setup a timer for 15 seconds
        timer = Timer(17.0, queue_time_out, args=(match,))
        timer.start()
    else:
        logger.debug('FAILED TO CREATE SQL MATCH')
        logger.debug(resp.reason)
        logger.debug(resp.content)

def queue_time_out(match):
    logger.debug(f"Queue timed out. Match will now be cancelled!")
    match['timedout'] = True
    pub.publish("match_responses", json.dumps(match))

def find_matches():
    """
    Loop over all players and look for potential opponents. Publish a match when 2 opponents have been found within eachothers ELO-range.

    The longer a player is queueing the bigger his ELO-range will be. This should reduce matchmaking time and can be tuned with variables in the config.
    It's important that a when a possible opponent is found that the player is also in the opponents player's ELO-range, this gives more balanced matches.
    It's also possible to cap out the ELO-range as to not get extreme ELO differences between players.
    """

    for player in matchmaking_pool.zscan_iter("matchmaking_time"):
        logger.info(f"------------------------------")
        logger.info(f"Checking: {player}")
        # Player id is the key
        player_id = player[0]

        # Using the player ID we can retrieve it's ELO.
        player_rank = matchmaking_pool.zscore("matchmaking_pool", player_id)
        logger.info(f"Player Rank: {player_rank}")
        # When the player has stopped queueing it won't be present
        if not player_rank and player_rank != 0.0:
            logger.info(f"No rank.... skipping: {player}")
            continue

        # The time at which the player queued is the value in the table.
        # Taking the difference between queue time and current time is the time the player has been in the queue.
        player_queue_time = player[1]
        player_time_in_queue = int(time.time()) - int(player_queue_time)

        # TODO: remove magic numbers, extract to config
        # minimum range = 1 and maximum range = 12
        # this should be sweet spot to make match making snappy and fair
        # after 30 seconds the range should widen to match against a wider range of opponents.
        player_rank_range = min(3 * (1 + 25 / 100) ** int(player_time_in_queue / 40), 12)
        logger.info(f"player_rank_range: {player_rank_range}")
        # Search all other players whose ELO is within the calculated range
        possible_opponents = []

        for opponent in matchmaking_pool.zrangebyscore(
            "matchmaking_pool",
            int(player_rank - player_rank_range),
            int(player_rank + player_rank_range),
            withscores=True,
        ):
            opponent_id = opponent[0]

            if opponent_id == player_id:
                continue

            logger.info(f"opponent: {opponent}")
            # Now we go the opposite direction, using the ID we retrieve the opponent's time in the queue.

            opponent_queue_time = matchmaking_pool.zscore(
                "matchmaking_time", opponent_id
            )

            # When the player has stopped queueing it won't be present
            if not opponent_queue_time:
                logger.info("No opponent_queue_time.... skipping: ", opponent)
                continue

            opponent_time_in_queue = int(time.time()) - int(opponent_queue_time)

            # TODO: remove magic numbers, extract to config
            # minimum range = 1 and maximum range = 12
            # this should be sweet spot to make match making snappy and fair
            # after 30 seconds the range should widen to match against a wider range of opponents.
            opponent_rank_range = min(3 * (1 + 25 / 100) ** int(player_time_in_queue / 40), 12)
            logger.info(f"opponent_rank_range: {opponent_rank_range}")
            opponent_rank = opponent[1]

            logger.info(f"Opponent Rank: {opponent_rank}")


            # Make sure the player's ELO is also in the opponent's ELO-range
            if (opponent_rank - opponent_rank_range) <= player_rank <= (
                opponent_rank + opponent_rank_range
            ) and player_id != opponent_id and len(possible_opponents) <= config.players_per_match-1:
                possible_opponent = {
                    "id": opponent_id,
                    "time_in_queue": opponent_time_in_queue,
                }
                possible_opponents.append(possible_opponent)

        if possible_opponents and len(possible_opponents) == config.players_per_match-1:
            logger.info(f"{len(possible_opponents)} possible opponents")
            logger.info(possible_opponents)
            # Sort opponents by time in queue from longest to shortest
            possible_opponents.sort(key=lambda x: x["time_in_queue"], reverse=True)

            # Remove both players from matchmaking pool
            matchmaking_pool.zrem("matchmaking_time", player_id)
            matchmaking_pool.zrem("matchmaking_pool", player_id)

            player_ids = [player_id]

            for i in range(len(possible_opponents)):
                logger.info(possible_opponents[i]["id"])
                matchmaking_pool.zrem("matchmaking_time", possible_opponents[i]["id"])
                matchmaking_pool.zrem("matchmaking_pool", possible_opponents[i]["id"])

                player_ids.append(possible_opponents[i]["id"])

            # Return the opponent that has been queueing the longest
            publish_match(player_ids)


def terminate(signal, frame):
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate)

    while True:
        logger.info("Finding matches...")

        find_matches()
        time.sleep(5)
