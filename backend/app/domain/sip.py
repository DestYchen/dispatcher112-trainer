import hashlib
import hmac
from pathlib import Path
from uuid import UUID

ROOT = Path("/data/telephony")
ACTIVE_STATES = ("REQUESTED", "RINGING", "CONNECTED")


def control_password(secret: str) -> str:
    return hmac.new(secret.encode(), b"sip-control-v1", hashlib.sha256).hexdigest()


def endpoint_name(user_id: UUID) -> str:
    return "u" + user_id.hex


def endpoint_password(secret: str, user_id: UUID, password_hash: str) -> str:
    data = f"sip-account-v1:{user_id}:{password_hash}"
    return hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()


def endpoint_config(user_id: UUID, password: str) -> str:
    name = endpoint_name(user_id)
    return (
        f"[{name}](training-endpoint)\naors={name}\nauth={name}\n"
        f"[{name}]\ntype=auth\nauth_type=userpass\nusername={name}\npassword={password}\n"
        f"[{name}]\ntype=aor\nmax_contacts=1\nremove_existing=yes\n"
        "qualify_frequency=15\nminimum_expiration=30\nmaximum_expiration=120\n\n"
    )


def speech_name(message: str) -> str:
    return hashlib.sha256(("espeak-ru-155-v1:" + message).encode()).hexdigest()


def prepare_speech(message: str) -> str:
    name = speech_name(message)
    directory = ROOT / "speech"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{name}.txt"
    if not target.exists():
        temporary = directory / f"{name}.pending"
        temporary.write_text(message, encoding="utf-8")
        temporary.replace(target)
    return name
