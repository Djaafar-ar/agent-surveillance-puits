#!/usr/bin/env python3
"""
Passerelle SMS pour la demonstration de l'agent de surveillance des puits.

Ce programme fait deux choses :
  1. il sert la page du simulateur (index.html) sur http://localhost:8787
  2. il recoit les demandes d'envoi de la page et les transmet a un telephone Android
     qui fait passerelle (application "SMS Gateway for Android", mode Local Server)

Utilisation :
  export SMS_GATEWAY_USER=VOTRE_LOGIN
  export SMS_GATEWAY_PASSWORD=VOTRE_MOT_DE_PASSE
  python passerelle-sms.py --phone 192.168.1.25
  python passerelle-sms.py --test                (aucun SMS envoye, tout est affiche a l'ecran)

Le mot de passe est lu dans la variable d'environnement SMS_GATEWAY_PASSWORD
(ou demande au clavier), jamais en argument : il ne reste pas dans l'historique du shell.

Puis ouvrir http://localhost:8787 dans le navigateur.
Aucune bibliotheque a installer : uniquement la bibliotheque standard de Python 3.
"""

import argparse
import base64
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

CFG = {"phone": None, "port_phone": 8080, "user": "", "password": "", "test": False, "sent": 0}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def send_sms(number, text):
    """Envoie un SMS via le telephone passerelle. Renvoie (ok, message)."""
    if CFG["test"] or not CFG["phone"]:
        log(f"MODE TEST - SMS non envoye vers {number} : {text.splitlines()[0]}")
        return True, "mode test"

    url = f"http://{CFG['phone']}:{CFG['port_phone']}/message"
    body = json.dumps({"textMessage": {"text": text}, "phoneNumbers": [number]}).encode("utf-8")
    auth = base64.b64encode(f"{CFG['user']}:{CFG['password']}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Basic " + auth},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
            CFG["sent"] += 1
            log(f"SMS envoye vers {number} (total : {CFG['sent']})")
            return True, "envoye"
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        log(f"ECHEC {e.code} vers {number} : {detail}")
        return False, f"HTTP {e.code} : {detail}"
    except Exception as e:  # reseau coupe, mauvaise adresse, telephone eteint
        log(f"ECHEC vers {number} : {e}")
        return False, str(e)


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):  # journal plus calme
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

    def _json(self, code, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.rstrip("/") == "/etat":
            return self._json(200, {"ok": True, "mode": "test" if CFG["test"] else "reel",
                                    "telephone": CFG["phone"], "envoyes": CFG["sent"]})
        if self.path == "/":
            self.path = "/index.html"
        return SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        if self.path.rstrip("/") != "/sms":
            return self._json(404, {"ok": False, "erreur": "chemin inconnu"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
            number = str(payload.get("to", "")).strip()
            text = str(payload.get("text", "")).strip()
        except Exception as e:
            return self._json(400, {"ok": False, "erreur": f"requete illisible : {e}"})
        if not number or not text:
            return self._json(400, {"ok": False, "erreur": "numero ou texte manquant"})
        ok, detail = send_sms(number, text)
        return self._json(200 if ok else 502, {"ok": ok, "detail": detail})


def main():
    ap = argparse.ArgumentParser(description="Passerelle SMS de demonstration")
    ap.add_argument("--phone", help="adresse IP locale du telephone passerelle, ex. 192.168.1.25")
    ap.add_argument("--phone-port", type=int, default=8080, help="port de l'application (8080 par defaut)")
    ap.add_argument("--user", default=os.environ.get("SMS_GATEWAY_USER", ""),
                    help="identifiant affiche par l'application (ou variable SMS_GATEWAY_USER)")
    ap.add_argument("--port", type=int, default=8787, help="port local de la passerelle (8787 par defaut)")
    ap.add_argument("--test", action="store_true", help="n'envoie aucun SMS, affiche seulement")
    args = ap.parse_args()

    test = args.test or not args.phone
    password = os.environ.get("SMS_GATEWAY_PASSWORD", "")
    if not test and not password:
        password = getpass.getpass("Mot de passe de la passerelle SMS : ")
    CFG.update({"phone": args.phone, "port_phone": args.phone_port, "user": args.user,
                "password": password, "test": test})

    here = os.path.dirname(os.path.abspath(__file__))
    os.chdir(here)
    if not os.path.exists("index.html"):
        log("ATTENTION : index.html n'est pas dans ce dossier. "
            "Mettez le fichier de la page a cote de ce programme.")

    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    log("Passerelle demarree.")
    log(f"Ouvrez la page ici : http://localhost:{args.port}")
    log("Mode : " + ("TEST, aucun SMS ne part" if CFG["test"] else f"REEL via le telephone {CFG['phone']}"))
    log("Dans la page, onglet Parametres : cocher l'envoi de vrais SMS et laisser l'adresse /sms.")
    log("Arreter avec Ctrl+C.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log(f"Arret. SMS envoyes : {CFG['sent']}.")
        srv.server_close()
        sys.exit(0)


if __name__ == "__main__":
    main()
