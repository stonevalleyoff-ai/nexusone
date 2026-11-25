"""Faux serveur de messagerie (IMAP) pour les tests : il note chaque commande reçue.

Il sait ce que NEXUS a le droit de demander (CAPABILITY, LOGIN, EXAMINE, UID SEARCH,
UID FETCH, LOGOUT). Tout le reste est noté puis refusé : un test peut ainsi prouver que la
boîte n'est jamais ouverte en écriture ni modifiée.
"""
from __future__ import annotations

import imaplib
import socket
import socketserver
import threading


class FakeImap:
    def __init__(self, user: str = 'camille@exemple.test', password: str = 'motdepasse-application'):
        self.user, self.password = user, password
        self.messages: list[bytes] = []          # le n-ième a l'UID n+1
        self.commands: list[str] = []
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def reply(self, text: str) -> None:
                self.wfile.write(text.encode() + b'\r\n')
                self.wfile.flush()

            def handle(self) -> None:
                self.reply('* OK faux serveur pret')
                logged = False
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    tag, _, rest = line.decode('utf-8', 'replace').strip().partition(' ')
                    words = rest.split()
                    verb = ' '.join(words[:2]).upper() if words and words[0].upper() == 'UID' else (words[0].upper() if words else '')
                    fake.commands.append(verb if verb != 'UID FETCH' else 'UID FETCH ' + ' '.join(words[3:]))
                    if verb == 'CAPABILITY':
                        self.reply('* CAPABILITY IMAP4rev1')
                        self.reply(f'{tag} OK fait')
                    elif verb == 'LOGIN':
                        logged = fake.user in rest and fake.password in rest
                        self.reply(f'{tag} OK bienvenue' if logged else f'{tag} NO identifiants refuses')
                    elif verb == 'LOGOUT':
                        self.reply('* BYE au revoir')
                        self.reply(f'{tag} OK fait')
                        return
                    elif not logged:
                        self.reply(f'{tag} NO non connecte')
                    elif verb == 'EXAMINE':
                        self.reply(f'* {len(fake.messages)} EXISTS')
                        self.reply(f'{tag} OK [READ-ONLY] boite ouverte en consultation')
                    elif verb == 'UID SEARCH':
                        self.reply('* SEARCH ' + ' '.join(str(i + 1) for i in range(len(fake.messages))))
                        self.reply(f'{tag} OK fait')
                    elif verb == 'UID FETCH' and words[2].isdigit() and 0 < int(words[2]) <= len(fake.messages):
                        uid = int(words[2])
                        limit = int(rest.rsplit('.', 1)[1].rstrip('>)')) if '<0.' in rest else 10**9
                        body = fake.messages[uid - 1][:limit]
                        self.wfile.write(f'* {uid} FETCH (UID {uid} BODY[]<0> {{{len(body)}}}\r\n'.encode() + body + b')\r\n')
                        self.reply(f'{tag} OK fait')
                    else:
                        self.reply(f'{tag} NO commande refusee')      # SELECT, STORE, APPEND, EXPUNGE, COPY…

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> 'FakeImap':
        self.thread.start()
        return self

    def __exit__(self, *_) -> None:
        self.server.shutdown()
        self.server.server_close()

    def connector(self, _account: dict) -> imaplib.IMAP4:
        """À la place de la connexion chiffrée : le faux serveur, en local."""
        return imaplib.IMAP4('127.0.0.1', self.port, timeout=10)

    def account(self, name: str = 'Gmail') -> dict:
        return {'nom': name, 'serveur': 'imap.exemple.test', 'port': 993, 'identifiant': self.user,
                'mot_de_passe': self.password}
