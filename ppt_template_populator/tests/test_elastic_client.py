from unittest.mock import MagicMock

import src.elastic_client as elastic_client


def test_connection_success():
    client = MagicMock()

    assert elastic_client.test_connection(client) == (True, "Connected")
    client.info.assert_called_once_with()


def test_connection_timeout():
    client = MagicMock()
    client.info.side_effect = elastic_client.ConnectionTimeout("timed out")

    connected, message = elastic_client.test_connection(client)

    assert connected is False
    assert "timed out" in message


def test_connection_authentication_failure(monkeypatch):
    class FakeAuthenticationError(Exception):
        pass

    monkeypatch.setattr(elastic_client, "AuthenticationException", FakeAuthenticationError)
    client = MagicMock()
    client.info.side_effect = FakeAuthenticationError()

    connected, message = elastic_client.test_connection(client)

    assert connected is False
    assert "rejected authentication" in message


def test_connection_refused():
    client = MagicMock()
    client.info.side_effect = elastic_client.ElasticsearchConnectionError("connection refused")

    connected, message = elastic_client.test_connection(client)

    assert connected is False
    assert "refused" in message
    assert "Kibana port" in message
