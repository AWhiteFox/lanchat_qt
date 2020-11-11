import socket
import json
from threading import Thread, Lock
from typing import Callable
from . import return_codes as codes
from .error_strings import ERROR_STRINGS


ENCODING = 'utf-8'
SIZE = 1024


class Client:
    def __init__(self):
        self.receive_callback = None
        self.close_callback = None
        self.username_limit = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.receive_thread = ReceiveThread(self.sock, self.on_receive, self.on_error)

    def connect(self, host: str, port: int):
        self.sock.connect((host, port))
        data = decode_packet(self.sock.recv(SIZE))
        code = data['code']
        if code < 0:
            raise NetworkingError(code)
        self.username_limit = data['username_limit']

    def authorize(self, username: str, receive_callback: Callable, close_callback: Callable):
        self.sock.send(encode_packet(code=codes.AUTHORIZE, username=username))
        data = decode_packet(self.sock.recv(SIZE))
        code = data['code']
        if code < 0:
            raise NetworkingError(code)
        self.receive_callback = receive_callback
        self.close_callback = close_callback
        self.receive_thread.start()
        return data['users']

    def send(self, message):
        self.sock.send(encode_packet(code=codes.MESSAGE, message=message))

    def get_username_limit(self):
        return self.username_limit

    def close(self, with_callback=False):
        self.receive_thread.stop()
        self.sock.close()
        if with_callback:
            self.close_callback()

    def on_receive(self, sock, payload: dict):
        code = payload['code']
        if code < 0:
            return
        self.receive_callback(payload)

    def on_error(self, sock):
        self.close(True)


class Server:
    def __init__(self):
        self.list_lock = Lock()
        self.closed = False
        self.username_limit = 16
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.accept_thread = ServerAcceptThread(self.sock, self.on_accept)
        self.connections = []
        self.usernames = []

    def bind(self, host: str, port: int):
        self.sock.bind((host, port))
        self.accept_thread.start()

    def get_username_limit(self) -> int:
        return self.username_limit

    def set_username_limit(self, value: int):
        self.username_limit = value

    def broadcast(self, binary):
        remove = []
        with self.list_lock:
            for conn in self.connections:
                try:
                    conn.send(binary)
                except OSError:
                    remove.append(conn)
        for conn in remove:
            self.disconnect(conn)

    def disconnect(self, sock, kicked=False):
        with self.list_lock:
            print('disconnect')
            i = self.connections.index(sock)
            username = self.usernames[i]
            sock.close()
            del self.connections[i]
            del self.usernames[i]
        code = codes.KICK if kicked else codes.DISCONNECT
        self.broadcast(encode_packet(code=code, username=username))

    def close(self):
        with self.list_lock:
            print('close')
            if self.closed:
                return

            self.accept_thread.stop()
            for conn in self.connections:
                conn.close()
            self.connections.clear()
            self.sock.close()
            self.closed = True

    def on_receive(self, sock: socket.socket, received_data: dict):
        with self.list_lock:
            print('on_receive')
            username = self.usernames[self.connections.index(sock)]
        self.broadcast(encode_packet(code=codes.MESSAGE,
                                     author=username,
                                     message=received_data['message']))

    def on_accept(self, conn: socket.socket):
        conn.send(encode_packet(code=codes.AUTHORIZE, username_limit=self.username_limit))
        try:
            data = decode_packet(conn.recv(SIZE))
        except json.JSONDecodeError:
            conn.send(encode_packet(code=codes.BAD_PAYLOAD))
            print('refuse')
            return
        username = data['username']
        if len(username) > self.username_limit:
            conn.send(encode_packet(code=codes.BAD_USERNAME, username_limit=self.username_limit))
            conn.close()
            return
        if username in self.usernames:
            conn.send(encode_packet(code=codes.USER_EXISTS))
            conn.close()
            return
        with self.list_lock:
            print('on_accept')
            conn.send(encode_packet(code=codes.OK, users=self.usernames))
            self.connections.append(conn)
            self.usernames.append(username)
        ReceiveThread(conn, self.on_receive, self.on_connection_close).start()
        self.broadcast(encode_packet(code=codes.CONNECT, username=username))

    def on_connection_close(self, sock: socket.socket):
        self.disconnect(sock)


class ReceiveThread(Thread):
    def __init__(self, sock: socket.socket, receive_callback: Callable, close_callback: Callable):
        super().__init__()
        self.sock = sock
        self.receive_callback = receive_callback
        self.close_callback = close_callback
        self.running = False

    def run(self):
        self.running = True
        try:
            while self.running:
                data = json.loads(self.sock.recv(SIZE).decode(ENCODING))
                self.receive_callback(self.sock, data)
        except (ConnectionAbortedError, ConnectionResetError, json.JSONDecodeError):
            self.running = False
            self.close_callback(self.sock)

    def stop(self):
        self.running = False


class ServerAcceptThread(Thread):
    def __init__(self, sock: socket.socket, accept_callback: Callable):
        super().__init__()
        self.running = False
        self.sock = sock
        self.accept_callback = accept_callback

    def run(self):
        self.running = True
        self.sock.listen(1)
        try:
            while self.running:
                conn, adr = self.sock.accept()
                self.accept_callback(conn)
        except OSError as e:
            self.running = False
            raise e

    def stop(self):
        self.running = False


class NetworkingError(Exception):
    def __init__(self, code: int):
        super().__init__(ERROR_STRINGS[code])


def encode_packet(**kwargs):
    if 'code' not in kwargs:
        raise ValueError("Packet should contain return code")
    return json.dumps(kwargs).encode(ENCODING)


def decode_packet(binary: bytes):
    return json.loads(binary.decode(ENCODING))
