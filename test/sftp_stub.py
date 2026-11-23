"""A real SFTP server for tests: paramiko's server side, serving a local
directory read-only on 127.0.0.1 with password auth. Lets the FTP-matching
code run its actual paramiko client path without JewishGen credentials.

    with SFTPStub(root, password="pw") as server:
        server.port  # connect as user "test"
"""

import os
import socket
import threading

import paramiko
from paramiko import SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface
from paramiko.sftp import SFTP_PERMISSION_DENIED

USER = "test"


class _Server(paramiko.ServerInterface):
    def __init__(self, password):
        self._password = password

    def check_auth_password(self, username, password):
        if username == USER and password == self._password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class _Handle(SFTPHandle):
    def stat(self):
        return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))


def _sftp_interface(root):
    class _Interface(SFTPServerInterface):
        def _local(self, path):
            local = os.path.realpath(os.path.join(root, path.lstrip("/")))
            if not (local == root or local.startswith(root + os.sep)):
                raise PermissionError(path)
            return local

        def _errno(self, exc):
            return SFTPServer.convert_errno(exc.errno) if getattr(exc, "errno", None) \
                else SFTP_PERMISSION_DENIED

        def list_folder(self, path):
            try:
                local = self._local(path)
                out = []
                for name in os.listdir(local):
                    attr = SFTPAttributes.from_stat(os.stat(os.path.join(local, name)))
                    attr.filename = name
                    out.append(attr)
                return out
            except OSError as e:
                return self._errno(e)

        def stat(self, path):
            try:
                return SFTPAttributes.from_stat(os.stat(self._local(path)))
            except OSError as e:
                return self._errno(e)

        lstat = stat

        def open(self, path, flags, attr):
            if flags & (os.O_WRONLY | os.O_RDWR):
                return SFTP_PERMISSION_DENIED
            try:
                f = open(self._local(path), "rb")
            except OSError as e:
                return self._errno(e)
            handle = _Handle(flags)
            handle.filename = path
            handle.readfile = f
            return handle

        def canonicalize(self, path):
            return "/" + path.strip("/")

    return _Interface


class SFTPStub:
    def __init__(self, root, password):
        self.root = os.path.realpath(root)
        self.password = password
        self.host_key = paramiko.RSAKey.generate(2048)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self.logins = 0
        self._transports = []
        self._stop = threading.Event()

    def __enter__(self):
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)
        for t in self._transports:
            t.close()
        self._sock.close()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(self.host_key)
            t.set_subsystem_handler("sftp", SFTPServer, _sftp_interface(self.root))
            t.start_server(server=_Server(self.password))
            self._transports.append(t)
            self.logins += 1
