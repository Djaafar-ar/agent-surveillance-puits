"""Dashboard accounts for one control room.

    python -m agent add-user --config config/config.yaml --name "K. Benali" --role operator
    python -m agent del-user --config config/config.yaml --name "K. Benali"

Roles
  viewer      sees everything, changes nothing
  operator    acknowledges alarms, writes the logbook (notes, well status,
              interventions), enters test results, takes and closes work orders
  supervisor  everything an operator does, plus: sends reports, validates or
              rejects well tests, creates and cancels work orders

Passwords are stored hashed (PBKDF2-SHA256, 200 000 rounds, random salt) in
data/users.json, never in the configuration. Every action on the dashboard is
written with the name of the person who did it.

Without any account, the v1 behaviour is kept: one shared password from
web.password_env (or none when the dashboard only listens on the local PC),
and actions are signed "poste local".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets

ROLES = ("viewer", "operator", "supervisor")
RANK = {r: i for i, r in enumerate(ROLES)}


def users_path(cfg) -> str:
    return os.path.join(os.path.dirname(cfg.storage["path"]), "users.json")


def _hash(pw: str, salt: bytes, rounds: int = 200_000) -> str:
    return base64.b64encode(hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, rounds)).decode()


class Users:
    def __init__(self, cfg):
        self.path = users_path(cfg)
        self.users = {}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.users = json.load(f)

    def __bool__(self):
        return bool(self.users)

    def save(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.users, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def add(self, name: str, role: str, password: str):
        name = name.strip()
        if not name or ":" in name:
            raise ValueError("invalid name")
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        if len(password) < 8:
            raise ValueError("password: at least 8 characters")
        salt = secrets.token_bytes(16)
        self.users[name] = {"role": role, "salt": base64.b64encode(salt).decode(), "hash": _hash(password, salt)}
        self.save()

    def delete(self, name: str):
        if self.users.pop(name, None) is None:
            raise ValueError(f"no user '{name}'")
        self.save()

    def check(self, name: str, password: str) -> str | None:
        """Role of the user if the password is right, else None."""
        u = self.users.get(name)
        if not u:
            _hash(password, b"0" * 16)          # same time whether the name exists or not
            return None
        if hmac.compare_digest(_hash(password, base64.b64decode(u["salt"])), u["hash"]):
            return u["role"]
        return None


def allowed(role: str, need: str) -> bool:
    return RANK.get(role, -1) >= RANK[need]
