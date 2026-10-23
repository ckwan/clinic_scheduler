"""Link tokens: 256 random bits, shown once (in the text), stored only as a SHA-256 hash.

The token is unguessable, so it needs no signature. Storing the hash means a database leak or a
log of the table cannot be used to open anyone's link, and a link can be revoked by flagging the
row. Expiry and single use are checked on the row, not encoded in the token.
"""

import hashlib
import secrets


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
