"""SMS and e-mail delivery.

Messages are queued and sent by a background thread with retries, so a slow
SMTP server or a GSM modem that is not answering never delays detection.
Every message and its delivery status is recorded in the database.

SMS channels
  console          prints the SMS and appends it to data/outbox/sms.log (tests, commissioning)
  android_gateway  Android phone running "SMS Gateway for Android" (local or cloud server)
  gsm_modem        USB/serial GSM modem with AT commands (recommended for a real site)

E-mail channels
  console          writes .eml files to data/outbox
  smtp             any SMTP server (STARTTLS or SSL)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import smtplib
import threading
import time
import unicodedata
import urllib.request
from datetime import datetime
from email.message import EmailMessage

from .config import secret

log = logging.getLogger("agent.notify")


def gsm_safe(text: str) -> str:
    """GSM 7-bit alphabet: strip accents so the SMS is readable on any phone."""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return t.replace("³", "3").replace("²", "2")


class ConsoleSms:
    name = "console"

    def __init__(self, cfg, outbox):
        self.path = os.path.join(outbox, "sms.log")

    def send(self, number, text):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} -> {number}\n{text}\n")
        log.info("SMS (console) to %s: %s", number, text.splitlines()[0])


class AndroidGatewaySms:
    """https://github.com/capcom6/android-sms-gateway (local or cloud server)."""
    name = "android_gateway"

    def __init__(self, cfg, outbox):
        self.url = cfg["url"]
        self.user = secret(cfg.get("user_env", "SMS_GATEWAY_USER"))
        self.pw = secret(cfg.get("password_env", "SMS_GATEWAY_PASSWORD"))

    def send(self, number, text):
        body = json.dumps({"textMessage": {"text": text}, "phoneNumbers": [number]}).encode()
        auth = base64.b64encode(f"{self.user}:{self.pw}".encode()).decode()
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Basic " + auth})
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()


class GsmModemSms:
    """GSM modem on a serial / USB port (e.g. COM3 on Windows, /dev/ttyUSB0 on Linux).
    Sends with AT+CMGS in text mode and reads replies with AT+CMGL so that an
    'OK' answered by the on-call engineer acknowledges the alarm."""
    name = "gsm_modem"

    def __init__(self, cfg, outbox):
        import serial  # pyserial
        self.serial = serial
        self.port = cfg["port"]
        self.baud = int(cfg.get("baud", 115200))
        self.pin = secret(cfg.get("pin_env")) if cfg.get("pin_env") else ""
        self.lock = threading.Lock()
        self.s = None

    def _open(self):
        if self.s is None or not self.s.is_open:
            self.s = self.serial.Serial(self.port, self.baud, timeout=2)
            self._at("AT")
            if self.pin:
                st = self._at("AT+CPIN?")
                if "SIM PIN" in st:
                    self._at(f"AT+CPIN={self.pin}", wait=5)
            self._at("AT+CMGF=1")
            self._at('AT+CSCS="GSM"')

    def _at(self, cmd, wait=1.0, end=b"\r"):
        self.s.reset_input_buffer()
        self.s.write(cmd.encode() + end)
        t = time.time()
        buf = b""
        while time.time() - t < wait + 5:
            buf += self.s.read(self.s.in_waiting or 1)
            if b"OK\r\n" in buf or b"ERROR" in buf or b">" in buf:
                break
        out = buf.decode(errors="ignore")
        if "ERROR" in out:
            raise IOError(f"{cmd} -> {out.strip()}")
        return out

    def send(self, number, text):
        with self.lock:
            self._open()
            r = self._at(f'AT+CMGS="{number}"', wait=3)
            if ">" not in r:
                raise IOError("modem did not accept the message")
            self.s.write(gsm_safe(text)[:600].encode("ascii", "ignore") + b"\x1a")
            t = time.time()
            buf = b""
            while time.time() - t < 30:
                buf += self.s.read(self.s.in_waiting or 1)
                if b"OK" in buf or b"ERROR" in buf:
                    break
            if b"+CMGS" not in buf:
                raise IOError("no delivery confirmation from the modem: " + buf.decode(errors="ignore").strip())

    def poll_replies(self):
        """Return [(phone, text)] of unread SMS and delete them from the SIM."""
        with self.lock:
            self._open()
            out = self._at('AT+CMGL="REC UNREAD"', wait=3)
            msgs = []
            lines = out.splitlines()
            for i, ln in enumerate(lines):
                if ln.startswith("+CMGL:"):
                    parts = ln.split(",")
                    phone = parts[2].strip('"') if len(parts) > 2 else ""
                    text = lines[i + 1] if i + 1 < len(lines) else ""
                    msgs.append((phone, text.strip()))
            if msgs:
                self._at("AT+CMGD=1,3", wait=3)   # delete read messages
            return msgs


class Dispatcher:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.L = cfg.language
        self.outbox = os.path.join(os.path.dirname(cfg.storage["path"]), "outbox")
        os.makedirs(self.outbox, exist_ok=True)
        t = cfg.sms.get("type", "console")
        self.smsc = {"console": ConsoleSms, "android_gateway": AndroidGatewaySms, "gsm_modem": GsmModemSms}[t](cfg.sms, self.outbox)
        self.q: queue.Queue = queue.Queue()
        self.engine = None
        self.journal_mem: list = []
        self.sent_sms = 0
        threading.Thread(target=self._worker, daemon=True, name="notify").start()
        if t == "gsm_modem":
            threading.Thread(target=self._poll_modem, daemon=True, name="modem-inbox").start()

    # ---- called by the engine ---------------------------------------------
    def journal(self, typ, label, text):
        t = self.engine.now if self.engine else time.time()
        self.journal_mem.append({"t": t, "type": typ, "label": label, "text": text})
        del self.journal_mem[:-300]
        if self.store:
            self.store.journal(t, typ, label, text)
        log.info("[%s] %s %s", typ, label or "", text)

    def sms(self, idx, text, alarm, kind):
        c = self.cfg.roster[idx]
        self.sent_sms += 1
        self.journal("sms", alarm.entity, f"SMS → {c.name} ({c.role}, {c.phone})")
        self.q.put(("sms", c.phone, text, None))

    def alarm_email(self, a, upgraded):
        lvl = "URGENCE" if a.severity == "urgence" else "AVERTISSEMENT"
        if self.L == "en":
            lvl = "EMERGENCY" if a.severity == "urgence" else "WARNING"
        tail = (" (aggravation)" if self.L == "fr" else " (escalated)") if upgraded else ""
        subj = f"[{lvl}] {a.entity}{' : ' if self.L == 'fr' else ': '}{a.title}{tail}"
        vals = "\n".join(f"  {k} = {v:.2f}" if isinstance(v, float) else f"  {k} = {v}" for k, v in a.snapshot.items() if k != "n")
        notified = ", ".join(self.cfg.roster[i].name for i in sorted(set(a.notified))) or "-"
        if self.L == "fr":
            body = (f"{a.entity}\nDétection : {datetime.fromtimestamp(a.t0):%d/%m/%Y %H:%M}\nNiveau : {lvl}\n"
                    f"Alarme : {a.title}, {a.value}\n\nAnalyse\n{a.diag}\n\nAction recommandée\n{a.action}\n\n"
                    f"Valeurs au moment de l'alarme\n{vals or '  -'}\n\nSMS envoyés à : {notified}\n\n"
                    "Message automatique de l'agent de surveillance (lecture seule, aucune commande envoyée au SCADA).")
        else:
            body = (f"{a.entity}\nDetected: {datetime.fromtimestamp(a.t0):%d/%m/%Y %H:%M}\nLevel: {lvl}\n"
                    f"Alarm: {a.title}, {a.value}\n\nAnalysis\n{a.diag}\n\nRecommended action\n{a.action}\n\n"
                    f"Values at alarm time\n{vals or '  -'}\n\nSMS sent to: {notified}\n\n"
                    "Automatic message from the surveillance agent (read-only, no command sent to the SCADA).")
        self.email(self.cfg.email.get("alarm_to", []), subj, body)

    def clear_email(self, a):
        dur = int((a.t_end - a.t0) / 60)
        ack = f"{self.cfg.roster[a.ack[0]].name} {datetime.fromtimestamp(a.ack[1]):%H:%M}" if a.ack else "-"
        if self.L == "fr":
            subj = f"Fin d'alarme {a.entity} : {a.title}"
            body = f"{a.entity}\nAlarme : {a.title}\nDurée : {dur} min\nAcquittée : {ack}\nAlarmes regroupées : {len(a.children)}\n\nLes valeurs sont revenues dans la plage normale."
        else:
            subj = f"Cleared {a.entity}: {a.title}"
            body = f"{a.entity}\nAlarm: {a.title}\nDuration: {dur} min\nAcknowledged: {ack}\nGrouped alarms: {len(a.children)}\n\nValues are back in the normal range."
        self.email(self.cfg.email.get("alarm_to", []), subj, body)

    def email(self, to, subject, body):
        if to:
            self.q.put(("email", list(to), subject, body))

    # ---- delivery ----------------------------------------------------------
    def _worker(self):
        while True:
            item = self.q.get()
            for attempt in range(3):
                try:
                    if item[0] == "sms":
                        self.smsc.send(item[1], item[2])
                        self._rec("sms", item[1], item[2].splitlines()[0], item[2], "sent")
                    else:
                        self._send_email(*item[1:])
                        self._rec("email", ", ".join(item[1]), item[2], item[3], "sent")
                    break
                except Exception as e:  # noqa: BLE001
                    log.warning("%s delivery failed (attempt %d): %s", item[0], attempt + 1, e)
                    if attempt == 2:
                        self._rec(item[0], item[1] if item[0] == "sms" else ", ".join(item[1]),
                                  item[2].splitlines()[0] if item[0] == "sms" else item[2], "", "failed", str(e))
                        if self.engine:
                            self.journal("info", None, f"{item[0].upper()} delivery FAILED: {e}")
                    else:
                        time.sleep(5 * (attempt + 1))

    def _rec(self, ch, rcpt, subj, body, status, err=""):
        if self.store:
            self.store.message(self.engine.now if self.engine else time.time(), ch, rcpt, subj, body, status, err)

    def _send_email(self, to, subject, body):
        e = self.cfg.email
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = e.get("from", "agent-surveillance@localhost")
        msg["To"] = ", ".join(to)
        msg.set_content(body)
        if e.get("type", "console") == "console":
            name = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".eml"
            with open(os.path.join(self.outbox, name), "wb") as f:
                f.write(bytes(msg))
            return
        host, port = e["smtp_host"], int(e.get("smtp_port", 587))
        user, pw = secret(e.get("user_env", "SMTP_USER")), secret(e.get("password_env", "SMTP_PASSWORD"))
        if e.get("ssl"):
            s = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            s = smtplib.SMTP(host, port, timeout=30)
            if e.get("starttls", True):
                s.starttls()
        try:
            if user:
                s.login(user, pw)
            s.send_message(msg)
        finally:
            s.quit()

    def _poll_modem(self):
        while True:
            time.sleep(15)
            try:
                for phone, text in self.smsc.poll_replies():
                    done = self.engine.ack_from_phone(phone, text) if self.engine else []
                    self.journal("info", None, f"SMS reçu de {phone} : « {text} »" + (f" → acquittement {done}" if done else ""))
            except Exception as e:  # noqa: BLE001
                log.debug("modem inbox: %s", e)

    def flush(self, timeout=10):
        t = time.time()
        while not self.q.empty() and time.time() - t < timeout:
            time.sleep(0.05)
        time.sleep(0.1)
