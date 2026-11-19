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

from typing import Optional

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


def _as_str(pid) -> str:
    """Redis hands back bytes; the JSON user blobs are keyed by str."""
    if isinstance(pid, (bytes, bytearray)):
        return pid.decode('utf-8')
    return str(pid)


def publish_match(player_ids: list, event: str):
    logger.debug(f"Publishing match for {len(player_ids)} players: {player_ids}")

    # create match sql record
    match_data = {
        'players': [asyncio.run(aget_user(_as_str(pid))) for pid in player_ids],
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
    # A decline already cancelled this pop when it landed. Sweeping it again 17s later
    # is not harmless: the fan-out is routed by USER id, so a player who requeued and
    # caught a NEW pop in the meantime got this stale cancel delivered on the new
    # socket — and, because not answering now means losing your place, it also dropped
    # them from a queue they had legitimately rejoined. The pop is settled; leave it.
    if match.get('proceeding') or 'DECLINED' in match['responses']:
        logger.debug(f"Match {match['id']} already resolved — skipping timeout sweep")
        return
    logger.debug(f"Queue timed out. Match will now be cancelled!")
    match['timedout'] = True
    pub.publish("match_responses", json.dumps(match))

def _waited_seconds(queued_at) -> int:
    """Seconds this entry has been in the pool (queue time is the zset SCORE)."""
    try:
        return max(0, int(time.time()) - int(float(queued_at)))
    except (TypeError, ValueError):
        return 0


def _rank_window(waited: int, thin_pool: bool) -> Optional[float]:
    """How far apart two ordinals may be before they are not comparable.

    Thin pool → None, meaning "do not filter": KiX's rule is that when only a couple of
    players are queued they should race each other regardless of the rank difference.
    A busy pool widens the window the longer someone waits (3 → 12 ordinals over ~2 min).
    """
    if thin_pool:
        return None
    return min(3 * (1 + 25 / 100) ** int(waited / 40), 12)


def _within(rank_a, rank_b, window) -> bool:
    if window is None:
        return True
    return (rank_b - window) <= rank_a <= (rank_b + window)


def find_matches():
    """Group queued players into one match of up to MATCH_MAX_PLAYERS runners.

    One pop, N players: as soon as MATCH_MIN_PLAYERS (default 2) compatible players are
    queued a match is created, and it fills to the max (default 6). Compatibility =
    a shared EVENT (a 1.5K runner cannot race a 5K runner) plus, when the pool is busy,
    an overlapping rank window. In a thin pool the rank window is dropped entirely, so
    two players at opposite ends of the ladder still get a race instead of both waiting.

    Every member of a group must share the SAME event — the intersection narrows as the
    group grows, which is why the event is only chosen once the group is closed.
    """
    queued = [(_as_str(pid), score) for pid, score in
              matchmaking_pool.zscan_iter("matchmaking_time")]
    numb_in_queue = len(queued)
    thin_pool = numb_in_queue <= config.thin_pool_threshold

    logger.info(f"------------------------------")
    logger.info(f"{numb_in_queue} in queue (thin pool: {thin_pool})")

    taken = set()

    # Everyone's rank is read once: zrange returns members ordered by ordinal.
    ranks = {_as_str(pid): score for pid, score in
             matchmaking_pool.zrange("matchmaking_pool", 0, -1, withscores=True)}

    for player_id, queued_at in queued:
        if player_id in taken:
            continue

        player_rank = ranks.get(player_id)
        if player_rank is None:
            logger.info(f"No rank in pool.... skipping: {player_id}")
            continue

        player_data = get_user(player_id)
        if not player_data:
            logger.error(f'No player data for {player_id}')
            continue

        player_events = set(player_data.get('events') or [])
        if not player_events:
            logger.info(f"No events.... skipping: {player_id}")
            continue

        waited = _waited_seconds(queued_at)
        window = _rank_window(waited, thin_pool)
        logger.info(f"Checking: {player_id} rank={player_rank} waited={waited}s window={window}")

        candidates = []
        for opponent_id, opponent_rank in ranks.items():
            if opponent_id == player_id or opponent_id in taken:
                continue

            opponent_data = get_user(opponent_id)
            if not opponent_data:
                logger.error(f'No player data for {opponent_id}')
                continue
            opponent_events = set(opponent_data.get('events') or [])
            if not opponent_events & player_events:
                continue

            opponent_waited = _waited_seconds(
                matchmaking_pool.zscore("matchmaking_time", opponent_id))

            # Both directions must agree: the candidate has to be inside this player's
            # window and this player inside the candidate's own (widened by their wait).
            if not thin_pool:
                opponent_window = _rank_window(opponent_waited, thin_pool)
                if not (_within(player_rank, opponent_rank, window)
                        and _within(opponent_rank, player_rank, opponent_window)):
                    continue

            candidates.append({
                'id': opponent_id,
                'rank': opponent_rank,
                'waited': opponent_waited,
                'events': opponent_events,
            })

        # Longest waiters first: the people who have been staring at the spinner get in.
        candidates.sort(key=lambda c: c['waited'], reverse=True)

        group = [player_id]
        common_events = set(player_events)
        for candidate in candidates:
            if len(group) >= config.max_players_per_match:
                break
            shared = common_events & candidate['events']
            if not shared:
                # no event common to the whole group — cannot add without splitting it
                continue
            group.append(candidate['id'])
            common_events = shared
            taken.add(candidate['id'])

        logger.info(f"possible_opponents: {group[1:]} "
                    f"(group size {len(group)}, common events {sorted(common_events)})")

        if len(group) < config.min_players_per_match:
            logger.info(f"Not enough players for a match yet ({len(group)})")
            continue

        # Give a partial group a moment to fill up: a burst of players joining (or the
        # app's own retry) should build one 6-way race, not three 1v1s. Once the oldest
        # member has waited fill_wait_seconds we take what we have — a thin queue must
        # still match the two people in it.
        if len(group) < config.max_players_per_match and waited < config.fill_wait_seconds:
            logger.info(f"Holding pop for {len(group)} players "
                        f"({config.fill_wait_seconds - waited}s left to fill, max {config.max_players_per_match})")
            continue

        # Everyone left in the group shares these; pick one fairly (not always the same).
        event = random.choice(sorted(common_events))
        logger.info(f"Match found: {len(group)} players for {event}")

        for pid in group:
            matchmaking_pool.zrem("matchmaking_time", pid)
            matchmaking_pool.zrem("matchmaking_pool", pid)
            ranks.pop(pid, None)
        taken.add(player_id)

        publish_match(group, event)


def terminate(signal, frame):
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate)

    while True:
        logger.info("Finding matches...")

        find_matches()
        time.sleep(5)
