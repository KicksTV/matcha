from redis.commands.search.field import TextField, NumericField, TagField
from redis.commands.search.indexDefinition import IndexDefinition, IndexType
from redis import Redis

from app.helpers.config import config

def create_indexes():
    r = Redis(host=config.redis_host, port=6379)
    schema = (TextField("$.user.username", as_name="username"), TagField("$.user.id", as_name="id"), NumericField("$.user.ordinal", as_name="ordinal"))
    r.ft().create_index(schema, definition=IndexDefinition(prefix=["user:"], index_type=IndexType.JSON))
