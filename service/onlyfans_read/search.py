import re
from .errors import BadRequest

def normalize_search(value):
    if value is None:return ''
    if type(value) is not str or len(value)>128 or any(ord(c)<32 for c in value):
        raise BadRequest('invalid_search','Use a display name, username or numeric fan ID, up to 128 characters.')
    query=value.strip().lstrip('@').casefold()
    if re.fullmatch(r'u[0-9]+',query):query=query[1:]
    return query
