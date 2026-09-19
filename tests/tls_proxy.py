# SPDX-License-Identifier: GPL-2.0-or-later
"""Test helper: TLS terminator in front of the plain relay, so wss:// can be
exercised locally. Generates a self-signed cert for 'localhost' and writes it
to $TMP/collab_test_cert.pem (point SSL_CERT_FILE at it)."""

import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading

listen_port, target_port = int(sys.argv[1]), int(sys.argv[2])
tmp = tempfile.gettempdir()
cert, key = os.path.join(tmp, "collab_test_cert.pem"), os.path.join(tmp, "collab_test_key.pem")
subprocess.run(
    ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "2",
     "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
    check=True, capture_output=True,
)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(cert, key)
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", listen_port))
srv.listen(8)
print(f"[tls-proxy] wss on {listen_port} -> {target_port}", flush=True)


def pump(a, b):
    try:
        while True:
            d = a.recv(1 << 20)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


while True:
    c, _ = srv.accept()
    try:
        tls = ctx.wrap_socket(c, server_side=True)
        up = socket.create_connection(("127.0.0.1", target_port))
        threading.Thread(target=pump, args=(tls, up), daemon=True).start()
        threading.Thread(target=pump, args=(up, tls), daemon=True).start()
    except (OSError, ssl.SSLError) as e:
        print("[tls-proxy]", e)
