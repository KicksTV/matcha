import asyncio
import json
import logging
import requests
import time
import signal
import secrets
import sys
import redis
import random

from threading import Timer

from app.helpers.db import add_match, get_match, aget_user, get_user
from app.helpers.config import config

logger = logging.getLogger("matcha_worker")
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
logger.addHandler(ch)

# Redis /1 is used for matchmaking pool
# Redis /2 is used to put matches found

matchmaking_pool = redis.Redis(**config.redis_options)
pub = redis.Redis(**config.redis_options)

def publish_match(player_ids: list, event: str):
    logger.debug("Publishing match...")

    # create match sql record
    match_data = {
        'players': [asyncio.run(aget_user(pid.decode('utf-8'))) for pid in player_ids],
        'status': 'STARTING',
        'event': event,
        'createdOn': time.time()
    }
    # logger.debug(json.dumps(match_data))
    try:
        # logger.debug(config.baseUrl)
        resp = requests.post(
            f'{config.baseUrl}/api/matches/init-match/', data=json.dumps(match_data), headers={'Content-Type': 'application/json', 'Authorization': f'Token {config.django_auth_token}'}, timeout=5)
        
        if resp and resp.ok:
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
    except Exception as e:
        logger.error('Failed to save sql match')
        logger.error(e)

    

def queue_time_out(match):
    match = asyncio.run(get_match(match['id']))
    if len(match['players']) == len(list(filter(lambda r: r != "", match["responses"]))):
        # everyone responded, no need to continue
        return
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
    players_in_queue = matchmaking_pool.zscan_iter("matchmaking_time")
    numb_in_queue = matchmaking_pool.zcount("matchmaking_pool", min=-100.0, max=100.0)
    for player in players_in_queue:
        logger.info(f"------------------------------")
        logger.info(f"Checking: {player}")
        # Player id is the key
        player_id = player[0]

        # Using the player ID we can retrieve it's ELO.
        player_rank = matchmaking_pool.zscore("matchmaking_pool", player_id)
        player_data = get_user(player_id.decode())
        if not player_data:
            logger.error(f'No player data for {player_id}')
            continue
        player_events = player_data['events']

        logger.info(f"Player Rank: {player_rank}")
        logger.info(f"Player Events: {player_events}")

        # When the player has stopped queueing it won't be present
        if not player_rank and player_rank != 0.0:
            logger.info(f"No rank.... skipping: {player}")
            continue

        # The time at which the player queued is the value in the table.
        # Taking the difference between queue time and current time is the time the player has been in the queue.
        player_queue_time = player[1]
        player_time_in_queue = int(time.time()) - int(player_queue_time)

        # only care about matching similar ranks when more than 50 people in queue
        print(numb_in_queue)
        if numb_in_queue > 50:
            # TODO: remove magic numbers, extract to config
            # minimum range = 1 and maximum range = 12
            # this should be sweet spot to make match making snappy and fair
            # after 30 seconds the range should widen to match against a wider range of opponents.
            player_rank_range = min(3 * (1 + 25 / 100) ** int(player_time_in_queue / 40), 12)

        else:
            player_rank_range = 100

        
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

            opponent_rank = opponent[1]

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

            if numb_in_queue > 50:

                # TODO: remove magic numbers, extract to config
                # minimum range = 1 and maximum range = 12
                # this should be sweet spot to make match making snappy and fair
                # after 30 seconds the range should widen to match against a wider range of opponents.
                opponent_rank_range = min(3 * (1 + 25 / 100) ** int(player_time_in_queue / 40), 12)
            else:
                opponent_rank_range = 100
            logger.info(f"opponent_rank_range: {opponent_rank_range}")

            logger.info(f"Opponent Rank: {opponent_rank}")


            # Make sure the player's ELO is also in the opponent's ELO-range
            if (opponent_rank - opponent_rank_range) <= player_rank <= (
                opponent_rank + opponent_rank_range
            ) and player_id != opponent_id and len(possible_opponents) <= config.players_per_match-1:
                
                opponent_data = get_user(opponent_id.decode())
                if not opponent_data:
                    logger.error(f'No player data for {opponent_id}')
                    continue
                opponent_events = opponent_data['events']

                logger.info(f"Opponent Events: {opponent_events}")

                # check if opponent wants to compeat in same events
                common_events = []
                for event in opponent_events:
                    if event in player_events:
                        common_events.append(event)
                
                if not len(common_events):
                    continue

                # randomly pick an event
                random_num = random.randint(0,(len(common_events)-1))

                event = common_events[random_num]

                possible_opponent = {
                    "id": opponent_id,
                    "time_in_queue": opponent_time_in_queue,
                }
                possible_opponents.append(possible_opponent)

                if len(possible_opponent) == config.players_per_match-1:
                    break

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
            publish_match(player_ids, event)


def terminate(signal, frame):
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate)

    while True:
        logger.info("Finding matches...")

        find_matches()
        time.sleep(5)
