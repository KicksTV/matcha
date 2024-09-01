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
from app.helpers.matchmaking import search_match, start_match, match_responses, match_finished, match_result
from app.helpers.config import config
from app.helpers.create_indexes import create_indexes

logger = logging.getLogger("matcha")
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
logger.addHandler(ch)


class StarletteCustom(Starlette):
    sessions = {"matches": {}}

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

class Queue(WebSocketEndpoint):
    encoding = "text"
    session_name = ""

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
        self.channel_name = "queue" # self.get_params(websocket).get('username', 'default_name')
        self.sessions = app.sessions

        await websocket.accept()

        if self.channel_name in self.sessions:
            self.sessions[self.channel_name].append(websocket)
        else:
            self.sessions[self.channel_name] = [websocket]

    async def on_disconnect(self, websocket: WebSocket, close_code: int):
        # logger.debug(
        #     f"Removing session {self.userDict['player']['fullName']} from queue."
        # )
        # self.sessions.pop(self.channel_name, None)
        # await self.broadcast_json(
        #     self.channel_name,
        #     json.dumps(
        #         {"type": "updateQueue", "action": "REMOVE", "user": self.userDict}
        #     )
        # )
        await websocket.close(code=1008)

    async def broadcast_message(self, msg):
        for k in self.sessions:
            ws = self.sessions[k]
            await ws.send_text(f"message text was: {msg}")

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
        if data['type'] == 'updateQueue':
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


async def match_response(websocket):
    # Get the id of the user from the query parameters
    user_id = str(websocket.query_params["user"])
    match_id = websocket.query_params["match"]
    response = websocket.query_params["response"]

    logger.debug(
        f"handle_match_response, match: {match_id} user: {user_id} response: {response}."
    )

    if not user_id or not match_id or not response:
        await websocket.close(code=1008)
        return

    await websocket.accept()

    await update_match_response(match_id, user_id, response)

    match = await get_match(match_id)

    logger.debug(f"Match responses so far {match}.")

    pub = aioredis.from_url("redis://localhost/2", decode_responses=True)

    if (
        len(list(filter(lambda r: r == "ACCEPTED", match["responses"])))
        >= config.players_per_match
    ):
        logger.debug(f"All players have ACCEPTED the queue pop.")
        await update_match_proceeding(match_id, proceeding=True)
        match['proceeding'] = True
        # notify if all responses have been received
        await start_match(match)
        # await pub.publish("match_responses", json.dumps(match))
        # await pub.close()

    user = await get_user(user_id)

    all_match_results = False
    while not all_match_results:
        # await for match results
        await match_result(match)

    # await for match finished
    await match_finished(match, user)


    
    await websocket.send_json(match)
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

    WebSocketRoute("/queue/", queue),
    WebSocketRoute("/queue-changes/", Queue),
    WebSocketRoute("/match-response/", match_response),

]

if config.environment == "debug":
    routes.append(Mount("/", app=StaticFiles(directory="static"), name="static"))

app = StarletteCustom(debug=True, routes=routes, on_startup=[startup])
