# All contributed tests are offline; accidental network calls must fail.
import socket
import pytest


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):

    def blocked(*args, **kwargs):
        raise AssertionError('Network access is forbidden in offline tests')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket.socket, 'connect_ex', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
