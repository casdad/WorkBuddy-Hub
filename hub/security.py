"""Credential vault and portable backups. No secret values are written to logs."""
import base64
import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(value: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    result = hashlib.scrypt(value.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return salt + ":" + result.hex()


def password_ok(value: str, stored: str) -> bool:
    if not isinstance(stored,str) or not re.fullmatch(r'[0-9a-f]{32}:[0-9a-f]{128}',stored):
        return False
    return hmac.compare_digest(password_hash(value, stored.split(":")[0]), stored)


def dpapi(data: bytes, protect: bool) -> bytes:
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    if protect:
        ok = ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        ok = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise RuntimeError("Windows 凭据保护失败；请使用加密备份迁移到其他用户或机器")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


class Vault:
    def __init__(self, folder: Path):
        self.path = folder / "vault.key"
        if not self.path.exists():
            self.install(Fernet.generate_key())
        raw = self.path.read_bytes()
        self.key = dpapi(raw[4:], False) if raw.startswith(b"DP01") else raw[4:]
        self.fernet = Fernet(self.key)

    def install(self, key: bytes):
        raw = b"DP01" + dpapi(key, True) if os.name == "nt" else b"FK01" + key
        temp = self.path.with_suffix(".tmp")
        temp.write_bytes(raw)
        temp.chmod(0o600)
        temp.replace(self.path)

    def encrypt(self, value: dict) -> str:
        return self.fernet.encrypt(json.dumps(value).encode()).decode()

    def decrypt(self, value: str) -> dict:
        return json.loads(self.fernet.decrypt(value.encode()))


def backup_key(passphrase: str, salt: bytes) -> bytes:
    if len(passphrase) < 12:
        raise ValueError("备份密码至少 12 个字符")
    raw = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, 600000)
    return base64.urlsafe_b64encode(raw)


def make_backup(store, vault: Vault, passphrase: str) -> bytes:
    salt = secrets.token_bytes(16)
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "backup.db"
        with store.connect() as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            # Login sessions do not survive export or restore.
            dst.execute("DELETE FROM sessions")
            dst.commit()
        body = json.dumps({"version": 1, "key": vault.key.decode(), "db": base64.b64encode(target.read_bytes()).decode()}).encode()
    return b"WBH1" + salt + Fernet(backup_key(passphrase, salt)).encrypt(body)


def restore_backup(data: bytes, passphrase: str, destination: Path):
    """Restore into a NEW directory only; never overwrite a running instance."""
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("恢复目标必须是新的或空的数据目录")
    if not data.startswith(b"WBH1"):
        raise ValueError("不是 WorkBuddy Hub 加密备份")
    body = json.loads(Fernet(backup_key(passphrase, data[4:20])).decrypt(data[20:]))
    if body.get("version") != 1:
        raise ValueError("不支持的备份版本")
    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp) / "hub.db"
        candidate.write_bytes(base64.b64decode(body["db"]))
        with closing(sqlite3.connect(candidate)) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("备份数据库校验失败")
            if conn.execute("PRAGMA user_version").fetchone()[0] not in (1,2,3):
                raise ValueError("数据库版本不兼容")
            crypt = Fernet(body["key"].encode())
            for (value,) in conn.execute("SELECT credential FROM accounts"):
                crypt.decrypt(value.encode())
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "hub.db").write_bytes(candidate.read_bytes())
        vault = Vault(destination)
        vault.install(body["key"].encode())
