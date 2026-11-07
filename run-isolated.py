"""Test-only launcher: disposable PG and loopback network, no service credentials."""
import os, sys, socket
keep={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL','TMPDIR','HOME','VAULT_TEST_VARIANT')}
os.environ.clear();os.environ.update(keep)
os.environ.update(ONLYFANS_TEST_PG_URL='postgresql+psycopg2://postgres@127.0.0.1:55461/postgres',
    ONLYFANS_TEST_PG_EMPTY_TEMPLATE='1',PYTHONDONTWRITEBYTECODE='1')
connect=socket.socket.connect
def local_only(sock,address):
    if isinstance(address,tuple) and address[0] not in ('127.0.0.1','::1','localhost'):
        raise RuntimeError('external network disabled in vault repair qualification')
    return connect(sock,address)
socket.socket.connect=local_only
socket.socket.connect_ex=lambda sock,address: local_only(sock,address) or 0
import pytest
raise SystemExit(pytest.main(sys.argv[1:]))
