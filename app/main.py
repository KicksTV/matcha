import asyncio
from asyncio import exceptions
import json
import logging
import redis
import traceback

from asyncio.exceptions import CancelledError
from asgiref.sync import sync_to_async
from urllib.parse import unquote
from redis import asyncio as aioredis
from redis.exceptions import ResponseError
from urllib.parse import unquote
from threading import Event, Thread


from starlette.applications import Starlette
from starlette.responses import Response, JSONResponse
from starlette.routing import Route, Mount, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.endpoints import WebSocketEndpoint
from starlette.websockets import WebSocket, WebSocketState
from starlette.middleware import Middleware
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.helpers.db import (
    update_ordinal,
    add_user,
    del_user,
    get_all_users,
    get_user_count,
    aget_user,
    update_games_played,
    update_match_response,
    insert_match_response,
    update_match_proceeding,
    get_match,
)
from app.helpers.elo import calculate_new_rating
from app.helpers.matchmaking import (
    search_match, start_match, match_responses, match_finished, match_result, _put_user_queue, _remove_user_queue)
from app.helpers.config import config
from app.helpers.create_indexes import create_indexes

logger = logging.getLogger("matcha")
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
logger.addHandler(ch)



def call_repeatedly(interval, func, *args):
    stopped = Event()
    def loop():
        while not stopped.wait(interval): # the first call is in `interval` secs
            asyncio.run(func(*args))
    Thread(target=loop).start()    
    return stopped.set


HEART_BEAT_INTERVAL = 3.0
async def is_websocket_active(ws: WebSocket) -> bool:
    if not (ws.application_state == WebSocketState.CONNECTED and ws.client_state == WebSocketState.CONNECTED):
        return False
    try:
        await asyncio.wait_for(ws.send_json({'type': 'ping'}), HEART_BEAT_INTERVAL)
        message = await asyncio.wait_for(ws.receive(), HEART_BEAT_INTERVAL)
        assert message['type'] == 'pong'
    except Exception as e:  # asyncio.TimeoutError and ws.close()
        logger.error(e)
        await ws.close()
    return True

def queue_data(request):
    'Return all the queue data'
    results = []
    try:
        results = get_all_users()
        logger.debug(results)
    except ResponseError as e:
        print(e)
        create_indexes()

    users = []
    for r in results:
        users.append(json.loads(r['json']))
    return JSONResponse(users)

def get_queue_count(request):
    'Return all the queue data'
    count = 0
    try:
        count = get_user_count()
        logger.debug(count)
    except ResponseError as e:
        print(e)
        create_indexes()
    logger.debug(f"queue count: {count}")
    return JSONResponse({"queueCount": count})

pub = redis.Redis(**config.redis_options)


logger.debug(config.redis_options)

class QueuePubSubListener(object):
    def __init__(self):
        self.clients = {}
        self.users = {}

        self.channel = 'queue'
        self.pubsub = pub.pubsub(ignore_subscribe_messages=False)
        self.pubsub.subscribe(**{self.channel: self.match_found_handler})
        self.thread = self.pubsub.run_in_thread(sleep_time=0.001)

    def register(self, client, user_id: str):
        if user_id not in self.clients:
            self.clients[user_id] = client
        else:
            logger.error(f'{user_id} already exists in QueuePubSubListener clients list. They should not be allowed to join queue if already searching!!!')

    def unregister(self, user_id: str):
        if user_id in self.clients:
            del self.clients[user_id]
        else:
            logger.error(f'{user_id} never existed in QueuePubSubListener clients list!!!!')

    def match_found_handler(self, message):
        msg = message['data']
        if msg is not None:
            logger.debug(f"(Reader) Queue Message Received: {msg}")
            match = json.loads(msg.decode())

            # notify all players that a match was found for them
            missing_players = []
            for player in match["players"]:
                pid = player['id']
                if pid in self.clients:
                    ws = self.clients[pid]
                    MatchMaking.handle_match_found(pid, ws, match)
                else:
                    missing_players.append(pid)

            if len(missing_players):
                logger.error(f'Player missing from QueuePubSubListener: {missing_players}')

        # if type(_message) != int:
        #     self.send(_message)

    # def send(self, data):
    #     for user_id, client in list(self.clients.items()):
    #         try:
    #             client.send(data)
    #         except Exception:
    #             del self.clients[user_id]
    #             del self.users[user_id]

    def add_user(self, user: dict):
        self.users[user['id']] = user
    
    def remove_user(self, user_id: str):
        try:
            del self.users[user_id]
        except Exception as e:
            logger.error(e)
            logger.debug(f'Failed to delete user from queue: {self.users}')

class MatchPubSubListener(object):
    def __init__(self):
        self.clients = {}
        self.users = {}
        self.channel = 'match_responses'
        self.pubsub = pub.pubsub(ignore_subscribe_messages=False)
        self.pubsub.subscribe(**{self.channel: self.handler_match_response})
        self.thread = self.pubsub.run_in_thread(sleep_time=0.001)

    def register(self, client, user_id: str):
        if user_id not in self.clients:
            self.clients[user_id] = client
        else:
            logger.error(f'{user_id} already exists in MatchPubSubListener clients list. They should not be allowed to join queue if already searching!!!')

    def unregister(self, user_id: str):
        if user_id in self.clients:
            del self.clients[user_id]
        else:
            logger.error(f'{user_id} never existed in MatchPubSubListener clients list!!!!')

    def handler_match_response(self, message):
        msg = message['data']
        if msg is not None:
            logger.debug(f"(Reader) Match Response Message Received: {msg}")
            match = json.loads(msg.decode())
            match_id = match['id']

            if 'timedout' in match and match['timedout']:
                responses = [r for r in match['responses'] if r != ""]
                if len(match['players']) == len(responses):
                    # already handled match
                    return
                logger.debug(f"Not all players responded in time. Match cancelled!")
                match['proceeding'] = False
                all_players_responded = True
                asyncio.run(update_match_proceeding(match_id, proceeding=False))

                for player in match['players']:
                    pid = player['id']
                    if pid in self.clients:
                        ws = self.clients[pid]
                        MatchMaking.handle_match_status(pid, ws, match)
                return

            all_players_responded = False

            if len(list(filter(lambda r: r == "ACCEPTED", match["responses"]))) >= config.players_per_match:
                logger.debug(f"All players have ACCEPTED the queue pop.")
                match['proceeding'] = True
                all_players_responded = True
            elif len(list(filter(lambda r: r != "", match["responses"]))) == config.players_per_match:
                logger.debug(f"All players have responded BUT not all accepted. Match cancelled!")
                match['proceeding'] = False
                all_players_responded = True
            
            if all_players_responded:
                # send players a message that the match will start
                missing_players = []
                for player in match['players']:
                    pid = player['id']
                    if pid in self.clients:
                        ws = self.clients[pid]
                        MatchMaking.handle_match_status(pid, ws, match)
                    else:
                        missing_players.append(pid)

                if len(missing_players):
                    logger.error(f'Player missing from MatchPubSubListener: {missing_players}')

        else:
            logger.error("Message is None!")
        # if type(_message) != int:
        #     self.send(_message)

    # def send(self, data):
    #     for client in self.clients:
    #         try:
    #             client.send(data)
    #         except Exception:
    #             self.clients.remove(client)

    def add_user(self, user: dict):
        self.users[user['id']] = user
    
    def remove_user(self, user_id: str):
        del self.users[user_id]
    


queue_listener = QueuePubSubListener()
match_listener = MatchPubSubListener()

match_making_sessions = {'match-making': []}

class MatchMaking(WebSocketEndpoint):

    is_alive = True
    received_pong = False
    userDict = {}
    ticker_task = None

    def get_params(self, websocket: WebSocket) -> dict:
        params_raw = websocket.get("query_string", b"").decode("utf-8")
        if '=' in params_raw:
            return {
                param.split("=")[0]: param.split("=")[1] for param in params_raw.split("&")
            }
        return {}

    async def on_connect(self, websocket: WebSocket):
        params = self.get_params(websocket)
        app = self.scope.get("app", None)
        self.channel_name = "match-making" # self.get_params(websocket).get('username', 'default_name')

        await websocket.accept()

        if self.channel_name in match_making_sessions:
            match_making_sessions[self.channel_name].append(websocket)
        else:
            match_making_sessions[self.channel_name] = [websocket]

    async def on_disconnect(self, websocket: WebSocket, close_code: int):
        user_id = None
        if self.userDict:
            try:
                user_id = self.userDict['id']
                logger.debug(
                    f"Removing session {self.userDict['player']['fullName']} from queue."
                )
            except:
                pass

        self.is_alive = False
        if self.ticker_task:
            self.ticker_task.cancel()

        if user_id:
            try:
                await del_user(user_id)
            except Exception as e:
                logger.debug(e)

            try:
                queue_listener.unregister(user_id)
                queue_listener.remove_user(user_id)
            except Exception as e:
                pass

            try:
                match_listener.unregister(user_id)
                match_listener.remove_user(user_id)
            except Exception as e:
                pass

            _remove_user_queue(user_id)

            await sync_to_async(self.broadcast_json)(
                self.channel_name,
                {"type": "updateQueue", "action": "REMOVE", "user": self.userDict}
            )
        await websocket.close(code=1008)

    async def on_receive(self, ws, data):
        # logger.debug(f"Received ws msg with data {data}.")
        data = json.loads(data)

        if data['type'] == 'pong':
            # logger.debug('Received PONG')
            self.received_pong = True
            return
        
        if data['type'] == 'joinQueue':
            await self.handle_join_queue(ws, data)
            return
        
        if data['type'] == 'leaveQueue':
            await self.handle_leave_queue(ws, data)
            return

        user = await aget_user(str(data['userId']))
        # logger.debug(f"Fetched user: {user}")

        if data['type'] == 'matchResponse':
            await self.handle_match_response(ws, data)
        elif data['type'] == 'matchResult':
            await self.handle_match_result(ws, data)
        elif data['type'] == 'updateQueue':
            if data['action'] == 'ADD':
                # notify about a new person joining queue
                await sync_to_async(self.broadcast_json)(
                    self.channel_name,
                    {"type": "updateQueue", "action": "ADD", "user": user}
                )
            elif data['action'] == 'REMOVE':
                 # notify about a new person joining queue
                await sync_to_async(self.broadcast_json)(
                    self.channel_name,
                    {"type": "updateQueue", "action": "REMOVE", "user": data['user']}
                )

    async def tick(self, ws: WebSocket) -> None:
        # counter = 0
        while self.is_alive:
            try:
                # logger.debug('Sending PING')
                await asyncio.wait_for(ws.send_json({"type": 'ping'}), HEART_BEAT_INTERVAL)
                # await asyncio.wait_for(self.received_pong(), HEART_BEAT_INTERVAL)

                counter = 0
                while not self.received_pong and counter < 5:
                    counter += 1
                    await asyncio.sleep(1)
                if counter >= 5:
                    logger.debug(f'WAITING FOR PONG TIMED OUT: {counter}')
                    raise exceptions.TimeoutError()
                # logger.debug(f'RECEIVED MSG: {msg}')
            except Exception as e:
                tracebac = traceback.format_exc()
                self.is_alive = False
                logger.debug(f'CLOSING WS, didn\'t receive pong: {e}: \n {tracebac}')
                await self.on_disconnect(ws, 1008)
            await asyncio.sleep(3)

    @staticmethod
    def add_session_to_match(ws: WebSocket, match_id: str):
        match_key = f'match:{match_id}'
        if match_key in match_making_sessions:
            match_making_sessions[match_key].append(ws)
        else:
            match_making_sessions[match_key] = [ws]

    @staticmethod
    def broadcast_json(channel: str,  json: dict):
        logger.debug(f'Received data for channel {channel} to broadcast: {json}')
        logger.debug(f'match_making_sessions: {match_making_sessions}')
        if channel in match_making_sessions:
            for ws in list(match_making_sessions[channel]):
                try:
                    asyncio.run(ws.send_json(json))
                except Exception as e:
                    logger.debug(e)
                    try:
                        index = match_making_sessions[channel].index(ws)
                        match_making_sessions[channel].pop(index)
                    except Exception as ee:
                        logger.debug(ee)
                        logger.debug(index)
                        logger.debug(match_making_sessions)



    @staticmethod
    def handle_match_found(user_id: str, ws: WebSocket, match: dict):
        try:
            asyncio.run(ws.send_json({'type': 'matchFound', 'match': match}))
        except:
            logger.error("Tried to send a match found response but Websocket is closed. Have we not removed a ws from queue?")
        user_dict = asyncio.run(aget_user(user_id))

        # subscribe to match listener
        match_listener.register(ws, user_id)
        match_listener.add_user(user_dict)


    @staticmethod
    def handle_match_status(user_id: str, ws: WebSocket, match: dict):
        asyncio.run(
            ws.send_json({
                'type': 'matchResponse',
                'proceeding': match['proceeding'],
                'timedout': match['timedout'] if 'timedout' in match else False,
                'match': match
            })
        )

        user = asyncio.run(aget_user(user_id))

        if match['proceeding']:
            # remove them from queue
            logger.debug(f"Match starting - remove player {user_id} from queue")
            queue_listener.unregister(user_id)
            queue_listener.remove_user(user_id)
            # match_listener.unregister(user_id)
            # match_listener.remove_user(user_id)

            asyncio.run(
                del_user(user_id))

            MatchMaking.broadcast_json('match-making',
                                    {"type": "updateQueue", "action": "REMOVE", "user": user})

            MatchMaking.add_session_to_match(ws, match['id'])
        else:
            index = None
            for i, player in enumerate(match['players']):
                if player['id'] == user_id:
                    index = i
            
            assert index is not None

            response = match['responses'][index]
            if response in ['DECLINED', '', 'AWAITING']:
                queue_listener.unregister(user_id)
                queue_listener.remove_user(user_id)

                asyncio.run(
                    del_user(user_id))
                
                MatchMaking.broadcast_json('match-making', {
                    'type': 'updateQueue',
                    'action': 'REMOVE',
                    "user": {
                        **user
                    }
                })

            else:
                # TODO: add them back into queue
                asyncio.run(_put_user_queue(user_id, user['ordinal'], user['queueStart']))

            match_listener.unregister(user_id)
            match_listener.remove_user(user_id)

    async def handle_leave_queue(self, ws, data):
        logger.debug('Removing user from queue')
        user_id = str(data["userId"])
        logger.debug(f"Removing user from queue: {data}.")
        if not user_id:
            await ws.close(code=1008)
        user = await aget_user(user_id)
        print("found user to remove: ", user)

        queue_listener.remove_user(user_id)
        queue_listener.unregister(user_id)

        if not user:
            return
        

        # logger.debug(f"Registered user into queue: {user}")

        sync_to_async(self.broadcast_json)('match-making', {
                'type': 'updateQueue',
                'action': 'REMOVE',
                'user': {
                **user
                }
            }
        )

        await _remove_user_queue(user_id)

        await del_user(user_id)

        

    async def handle_join_queue(self, ws, data):
        self.userDict = data['user']
        logger.debug(self.userDict)
        user_id = str(self.userDict["id"])
        self.userDict["id"] = user_id
        player = self.userDict["player"]
        ordinal = float(player["ordinal"])
        events = self.userDict["events"]

        # logger.debug(f"Rating for {user_id} is {ordinal}.")
        if not user_id:
            await ws.close(code=1008)
        user = await aget_user(user_id)
        # Add user to databaase as guest if not yet registered
        if not user:
            user = await add_user(self.userDict)
        else:
            # logger.debug(f"Updating ordinal {ordinal} for {user_id}.")
            await update_ordinal(user_id, ordinal)
        
        await _put_user_queue(user_id, ordinal)
        queue_listener.add_user(user)
        # register to queue listener
        queue_listener.register(ws, user_id)

        # logger.debug(f"Registered user into queue: {user}")

        await sync_to_async(self.broadcast_json)('match-making', {
                'type': 'updateQueue',
                'action': 'ADD',
                'user': {**user}
            }
        )

        # check to make sure connection is alive
        self.ticker_task = asyncio.create_task(self.tick(ws))

    async def handle_match_response(self, ws: WebSocket, data: dict):
        user_id = str(data['userId'])
        match_id = data['matchId']
        response = data['response']

        match = await get_match(match_id)

        index = None
        for i, player in enumerate(match['players']):
            pid = player['id']
            if pid == user_id:
                index = i
        
        assert index is not None

        await insert_match_response(match_id, user_id, response, index)

        match['responses'][index] = response

        logger.debug(
            f"handle_match_response, match: {match_id} user: {user_id} response: {response}."
        )
        pub = aioredis.from_url(f"{config.redis_url}", decode_responses=True)
        await pub.publish("match_responses", json.dumps(match))
        await pub.close()

    async def handle_match_result(self, ws: WebSocket, result: dict):
        logger.debug("Received match result")
        # {
        #   'type': 'matchResult',
        #   'playerId': `${userID.value}`,
        #   'match': 242,
        #    'playerResult': {
        #        'season': '2024-25',
        #        'player': `${userID.value}`,
        #        'place': 2,
        #        'performance': "180.0",
        #        'formattedPerformance': "3:00.00",
        #        'disqualification': '',
        #     }
        # }
        try:
            match_id = result['matchId']
            # await sync_to_async(self.broadcast_json)(
            #     f'match:{match_id}',
            #     {"type": "matchResult", "result": result}
            # )
            await sync_to_async(self.broadcast_json)(
                f'match:{match_id}',
                result
            )
        except Exception as e:
            logger.debug(f'Got error: {e}')
            logger.debug(f'Received result: {result}')




# async def queue(websocket):
#         user = str(websocket.query_params["user"])
#         logger.debug(user)
#         userDict = json.loads(user)
#         logger.debug(userDict)
#         user_id = str(userDict["id"])
#         player = userDict["player"]
#         ordinal = player["ordinal"]
#         ordinal = float(ordinal)
#         logger.debug(f"Rating for {user_id} is {ordinal}.")
#         if not user_id:
#             await websocket.close(code=1008)

#         user = await aget_user(user_id)

#         # Add user to databaase as guest if not yet registered
#         if not user:
#             user = await add_user(userDict)
#         else:
#             logger.debug(f"Updating ordinal {ordinal} for {user_id}.")
#             await update_ordinal(user_id, ordinal)
        
#         await websocket.accept()

#         logger.debug(f"Adding {user} to the queue.")

#         # Add user to the queue
#         search_match_task = asyncio.create_task(search_match(user))
        
#         async def check_queue_status(func):
#             is_alive = await func
#             if not is_alive:
#                 print(f"connection no longer alive, removing {user} from queue")
#                 # we cancel if we find the connection has been disconnected
#                 search_match_task.cancel()
#                 await del_user(user_id)
#                 await websocket.close()
#             return is_alive
            
            
#         async def check_queue_status_again():
#             try:
#                 await asyncio.sleep(3)
#                 check_ws_alive_task = asyncio.create_task(is_websocket_active(websocket))
#                 is_alive = await check_queue_status(check_ws_alive_task)
#                 return is_alive
#             except CancelledError:
#                 print("Is alive check cancelled probably found match!")
#                 return False

#         async def aa():
#             while not search_match_task.done():
#                 is_alive = await check_queue_status_again()
#                 if not is_alive:
#                     break
                
#             print("not search_match_task.done()", not search_match_task.done())
#             if not is_alive and not search_match_task.done():
#                 return None
#             return search_match_task.result()

        
#         aaa = asyncio.create_task(aa())

#         def found_match(task):
#             print("found match, cancel is_alive check")
#             aaa.cancel()
#         search_match_task.add_done_callback(found_match)

#         match = await aaa

#         if not match:
#             print("is_alive is false")
#             return


#         logger.debug(f"Got a match response: {match}.")
#         # Return room  id to user when match is found
#         match_response = {"type": "matchFound", "match": match}
#         logger.debug(f"Got a match response: {match_response}.")
#         await websocket.send_json(match_response)

#         # create the match in sql
        

#         # now listen for match responses
#         # await for response to accepting the match
#         match = await match_responses(match['id'], user_id)
#         # logger.debug(f"{match}.")

#         match_response = {
#             'type': 'matchResponse',
#             'proceeding': match['proceeding'],
#             'match': match
#         }
#         if match['proceeding']:
#             logger.debug(f"Match is proceeding: {match_response}.")
#         else:
#             logger.debug(f"Match will not be proceeding: {match_response}.")
            
#         await websocket.send_json(match_response)
#         await del_user(user_id)
#         await websocket.close()


def startup():
    logger.info("Fueled up and ready to roll out!")


if config.environment == "debug":
    allowed_hosts=['127.0.0.1:8002', '172.23.109.48:8002', '192.168.0.38:8002']
else:
    allowed_hosts=['*']

middleware = [
    Middleware(
        TrustedHostMiddleware,
        allowed_hosts=allowed_hosts,
    ),
    Middleware(HTTPSRedirectMiddleware)
]

routes = [
    Route("/queue-data/", queue_data, methods=["GET"]),
    Route("/queue-count/", get_queue_count, methods=["GET"]),
    WebSocketRoute("/match-making/", MatchMaking),
]

if config.environment == "debug":
    routes.append(Mount("/", app=StaticFiles(directory="static"), name="static"))

app = Starlette(debug=True, routes=routes, on_startup=[startup])
