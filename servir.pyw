import functools
import http.server
import os
import sys

# pythonw não tem stdout/stderr; sem isto o servidor falha ao registar cada pedido
sys.stdout = sys.stderr = open(os.devnull, "w")

PASTA = os.path.dirname(os.path.abspath(__file__))
handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=PASTA)
try:
    http.server.ThreadingHTTPServer(("127.0.0.1", 8765), handler).serve_forever()
except OSError:
    pass  # a porta já está em uso: o servidor já está a correr
