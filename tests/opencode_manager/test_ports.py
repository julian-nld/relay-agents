import socket

from opencode_manager import ports


def listener():
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen()
    return server, server.getsockname()[1]


def test_a_listening_port_is_taken():
    server, port = listener()
    with server:
        assert not ports.is_free(port)


def test_a_port_is_free_right_after_its_service_stopped():
    server, port = listener()
    client = socket.create_connection(("127.0.0.1", port))
    connection, _ = server.accept()
    connection.close()  # the service closes first: its side waits (TIME_WAIT) on the port
    client.close()
    server.close()
    assert ports.is_free(port)
