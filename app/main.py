import asyncio
import json
import logging
import redis

from asyncio.exceptions import CancelledError
from urllib.parse import unquote
from redis import asyncio as aioredis
from redis.exceptions import ResponseError

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
    get_user,
    update_games_played,
    update_match_response,
    update_match_proceeding,
    get_match,
)
from app.helpers.elo import calculate_new_rating
from app.helpers.matchmaking import search_match, start_match, match_responses, match_finished, match_result, _put_user_queue
from app.helpers.config import config
from app.helpers.create_indexes import create_indexes

logger = logging.getLogger("matcha")
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
logger.addHandler(ch)


class StarletteCustom(Starlette):
    sessions = {"match-making": []}

HEART_BEAT_INTERVAL = 5
async def is_websocket_active(ws: WebSocket) -> bool:
    if not (ws.application_state == WebSocketState.CONNECTED and ws.client_state == WebSocketState.CONNECTED):
        return False
    try:
        await asyncio.wait_for(ws.send_json({'type': 'ping'}), HEART_BEAT_INTERVAL)
        message = await asyncio.wait_for(ws.receive_json(), HEART_BEAT_INTERVAL)
        assert message['type'] == 'pong'
    except BaseException:  # asyncio.TimeoutError and ws.close()
        return False
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

pub = redis.Redis(host="redis", port=6379, db=2)

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
            for pid in match["players"]:
                if pid in self.clients:
                    ws = self.clients[pid]
                    ws.handle_match_found(ws, match)
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
        del self.users[user_id]

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
            logger.error(f'{user_id} already exists in QueuePubSubListener clients list. They should not be allowed to join queue if already searching!!!')

    def unregister(self, user_id: str):
        if user_id in self.clients:
            del self.clients[user_id]
        else:
            logger.error(f'{user_id} never existed in QueuePubSubListener clients list!!!!')

    def handler_match_response(self, message):
        msg = message['data']
        if msg is not None:
            logger.debug(f"(Reader) Match Response Message Received: {msg}")
            match = json.loads(msg.decode())

            match_id = match['match']
            user_id = match['user']
            response = match['response']

            asyncio.run(update_match_response(match_id, user_id, response))

            match = asyncio.run(get_match(match_id))


            all_players_responded = False

            if 'timedout' in match and match['timedout']:
                logger.debug(f"Not all players responded in time. Match cancelled!")
                match['proceeding'] = False
                all_players_responded = True
                asyncio.run(update_match_proceeding(match_id, proceeding=True))

            elif len(list(filter(lambda r: r == "ACCEPTED", match["responses"]))) >= config.players_per_match:
                logger.debug(f"All players have ACCEPTED the queue pop.")
                match['proceeding'] = True
                all_players_responded = True
            elif len(match["responses"]) == config.players_per_match:
                logger.debug(f"All players have responded BUT not all accepted. Match cancelled!")
                match['proceeding'] = False
                all_players_responded = True
            
            if all_players_responded:
                # send players a message that the match will start
                missing_players = []
                for pid in match['players']:
                    if pid in self.clients:
                        ws = self.clients[pid]
                        ws.handle_match_status(ws, match)
                    else:
                        missing_players.append(pid)

                if len(missing_players):
                    logger.error(f'Player missing from MatchPubSubListener: {missing_players}')

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
from urllib.parse import unquote
class MatchMaking(WebSocketEndpoint):

    match_groups = {}

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
        self.sessions = app.sessions

        user = unquote(params["user"])
        logger.debug(user)
        userDict = json.loads(user)
        self.userDict = userDict
        logger.debug(userDict)
        user_id = str(userDict["id"])
        player = userDict["player"]
        ordinal = player["ordinal"]
        ordinal = float(ordinal)
        logger.debug(f"Rating for {user_id} is {ordinal}.")
        if not user_id:
            await websocket.close(code=1008)

        user = await get_user(user_id)

        # Add user to databaase as guest if not yet registered
        if not user:
            user = await add_user(userDict)
        else:
            logger.debug(f"Updating ordinal {ordinal} for {user_id}.")
            await update_ordinal(user_id, ordinal)

        
        await _put_user_queue(user_id, ordinal)

        # register to queue listener
        queue_listener.register(websocket, user_id)

        logger.debug(f"Registered user into queue: {user}")

        await websocket.accept()

        if self.channel_name in self.sessions:
            self.sessions[self.channel_name].append(websocket)
        else:
            self.sessions[self.channel_name] = [websocket]

    async def on_disconnect(self, websocket: WebSocket, close_code: int):
        user_id = self.userDict['id']
        logger.debug(
            f"Removing session {self.userDict['player']['fullName']} from queue."
        )
        try:
            queue_listener.unregister(user_id)
            queue_listener.remove_user(user_id)
        except Exception as e:
            print(e)

        try:
            match_listener.unregister(user_id)
            match_listener.remove_user(user_id)
        except Exception as e:
            print(e)

        # self.sessions.pop(self.channel_name, None)
        await self.broadcast_json(
            self.channel_name,
            json.dumps(
                {"type": "updateQueue", "action": "REMOVE", "user": self.userDict}
            )
        )
        await websocket.close(code=1008)

    async def broadcast_json(self, channel: str,  json: str):
        if channel in self.sessions:
            for ws in list(self.sessions[channel]):
                try:
                    await ws.send_json(json)
                except Exception as e:
                    logger.debug(e)
                    index = self.sessions[channel].index(ws)
                    self.sessions[channel].pop(index)

    async def on_receive(self, ws, data):
        logger.debug(f"Received ws msg with data {data}.")
        data = json.loads(data)

        user = await get_user(data['user']['id'])

        if data['type'] == 'MatchResponse':
            await self.handle_match_response(ws, data)
        elif data['type'] == 'MatchResult':
            await self.handle_match_result(ws, data)
        elif data['type'] == 'updateQueue':
            if data['action'] == 'ADD':
                # notify about a new person joining queue
                await self.broadcast_json(
                    self.channel_name,
                    {"type": "updateQueue", "action": "ADD", "user": user}
                )
            elif data['action'] == 'REMOVE':
                 # notify about a new person joining queue
                await self.broadcast_json(
                    self.channel_name,
                    {"type": "updateQueue", "action": "REMOVE", "user": data['user']}
                )

    def add_session_to_match(self, ws: WebSocket, match_id: str):
        if match_id in self.match_groups:
            self.sessions[f'match:{match_id}'].append(ws)
        else:
            self.sessions[f'match:{match_id}'] = [ws]

    def handle_match_found(self, ws: WebSocket, match: dict):
        user_id = self.userDict['id']
        self.send({
            'type': 'MatchFound',
            'match': match
        })

        # subscribe to match listener
        match_listener.register(ws, user_id)
        match_listener.add_user(user_id)

    async def handle_match_status(self, ws: WebSocket, match: dict):
        user_id = self.userDict['id']
        ws.send({
            'type': 'MatchResponse',
            'proceeding': match['proceeding'],
            'match': match
        })

        if match['proceeding']:
            # remove them from queue
            queue_listener.unregister(user_id)
            queue_listener.remove_user(user_id)

            self.add_session_to_match(ws, match['id'])
        else:
            # TODO: add them back into queue
            pass

    async def handle_match_response(self, ws: WebSocket, response: dict):
        user_id = response['user']
        match_id = response['match']
        resp = response['response']
        logger.debug(
            f"handle_match_response, match: {match_id} user: {user_id} response: {resp}."
        )
        pub = aioredis.from_url("redis://localhost/2", decode_responses=True)
        await pub.publish("match_responses", json.dumps(response))
        await pub.close()

    async def handle_match_result(self, ws: WebSocket, result: dict):
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
        match_id = result['match']
        await self.broadcast_json(
            f'match:{match_id}',
            json.dumps(
                {"type": "MatchResult", "result": result}
            )
        )





async def queue(websocket):
        user = str(websocket.query_params["user"])
        logger.debug(user)
        userDict = json.loads(user)
        logger.debug(userDict)
        user_id = str(userDict["id"])
        player = userDict["player"]
        ordinal = player["ordinal"]
        ordinal = float(ordinal)
        logger.debug(f"Rating for {user_id} is {ordinal}.")
        if not user_id:
            await websocket.close(code=1008)

        user = await get_user(user_id)

        # Add user to databaase as guest if not yet registered
        if not user:
            user = await add_user(userDict)
        else:
            logger.debug(f"Updating ordinal {ordinal} for {user_id}.")
            await update_ordinal(user_id, ordinal)
        
        await websocket.accept()

        logger.debug(f"Adding {user} to the queue.")

        # Add user to the queue
        search_match_task = asyncio.create_task(search_match(user))
        
        async def check_queue_status(func):
            is_alive = await func
            if not is_alive:
                print(f"connection no longer alive, removing {user} from queue")
                # we cancel if we find the connection has been disconnected
                search_match_task.cancel()
                await del_user(user_id)
                await websocket.close()
            return is_alive
            
            
        async def check_queue_status_again():
            try:
                await asyncio.sleep(3)
                check_ws_alive_task = asyncio.create_task(is_websocket_active(websocket))
                is_alive = await check_queue_status(check_ws_alive_task)
                return is_alive
            except CancelledError:
                print("Is alive check cancelled probably found match!")
                return False

        async def aa():
            while not search_match_task.done():
                is_alive = await check_queue_status_again()
                if not is_alive:
                    break
                
            print("not search_match_task.done()", not search_match_task.done())
            if not is_alive and not search_match_task.done():
                return None
            return search_match_task.result()

        
        aaa = asyncio.create_task(aa())

        def found_match(task):
            print("found match, cancel is_alive check")
            aaa.cancel()
        search_match_task.add_done_callback(found_match)

        match = await aaa

        if not match:
            print("is_alive is false")
            return


        logger.debug(f"Got a match response: {match}.")
        # Return room  id to user when match is found
        match_response = {"type": "matchFound", "match": match}
        logger.debug(f"Got a match response: {match_response}.")
        await websocket.send_json(match_response)

        # create the match in sql
        

        # now listen for match responses
        # await for response to accepting the match
        match = await match_responses(match['id'], user_id)
        # logger.debug(f"{match}.")

        match_response = {
            'type': 'matchResponse',
            'proceeding': match['proceeding'],
            'match': match
        }
        if match['proceeding']:
            logger.debug(f"Match is proceeding: {match_response}.")
        else:
            logger.debug(f"Match will not be proceeding: {match_response}.")
            
        await websocket.send_json(match_response)
        await del_user(user_id)
        await websocket.close()


def startup():
    logger.info("Fueled up and ready to roll out!")


if config.environment == "debug":
    allowed_hosts=['127.0.0.1:8002', 'localhost:8002', '192.168.0.38:8002']
else:
    allowed_hosts=['https://running-app-efd09e797a8a.herokuapp.com/']

middleware = [
    Middleware(
        TrustedHostMiddleware,
        allowed_hosts=allowed_hosts,
    ),
    Middleware(HTTPSRedirectMiddleware)
]

routes = [
    Route("/queue-data/", queue_data, methods=["GET"]),
    WebSocketRoute("/match-making/", MatchMaking),
]

if config.environment == "debug":
    routes.append(Mount("/", app=StaticFiles(directory="static"), name="static"))

app = StarletteCustom(debug=True, routes=routes, on_startup=[startup])
