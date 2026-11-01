"""Qualification-only network boundary; never imported by production."""
import socket
import ipaddress
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex

def _allowed(address):
    if not isinstance(address, tuple):
        return True
    host = address[0]
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

def connect(self, address):
    if not _allowed(address):
        raise OSError('qualification denies non-loopback network')
    return _original_connect(self, address)

def connect_ex(self, address):
    if not _allowed(address):
        raise OSError('qualification denies non-loopback network')
    return _original_connect_ex(self, address)

socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
