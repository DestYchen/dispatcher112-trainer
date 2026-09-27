"""Local PBX supervisor: configuration reload and preparation of Russian speech."""

import hashlib
import hmac
import ipaddress
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

os.umask(0o007)
RUNTIME = Path('/run/asterisk')
RUNTIME.mkdir(parents=True, exist_ok=True)
ROOT = Path('/data/telephony')
ROOT.mkdir(parents=True, exist_ok=True)
for name in ('speech', 'recordings'):
    (ROOT / name).mkdir(exist_ok=True)
accounts = ROOT / 'accounts.conf'
accounts.touch(exist_ok=True)
password = hmac.new(os.environ['JWT_SECRET'].encode(), b'sip-control-v1', hashlib.sha256).hexdigest()
(RUNTIME / 'ari.conf').write_text(
    '[general]\nenabled=yes\npretty=no\n[dispatcher]\ntype=user\nread_only=no\n'
    f'password={password}\n', encoding='utf-8',
)
address = str(ipaddress.ip_address(os.environ.get('SIP_MEDIA_ADDRESS', '127.0.0.1')))
local = socket.gethostbyname(socket.gethostname())
(RUNTIME / 'rtp.conf').write_text(
    '[general]\nrtpstart=10000\nrtpend=10199\nicesupport=yes\nstrictrtp=yes\n'
    f'[ice_host_candidates]\n{local} => {address}\n', encoding='utf-8',
)
process = subprocess.Popen(['asterisk', '-f', '-g'])


def stop(signum, frame):
    process.terminate()


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
previous = None
while process.poll() is None:
    current = hashlib.sha256(accounts.read_bytes()).digest()
    if current != previous:
        result = subprocess.run(['asterisk', '-rx', 'module reload res_pjsip.so'], capture_output=True)
        if result.returncode == 0 and b'No such command' not in result.stdout:
            previous = current
    for source in (ROOT / 'speech').glob('*.txt'):
        target = source.with_suffix('.wav')
        if target.exists():
            continue
        temporary = source.with_suffix('.tmp.wav')
        result = subprocess.run(
            ['espeak-ng', '-v', 'ru', '-s', '155', '-f', str(source), '-w', str(temporary)],
            capture_output=True,
        )
        if result.returncode == 0:
            converted = subprocess.run(
                ['sox', str(temporary), '-r', '8000', '-c', '1', '-b', '16', str(target)],
                capture_output=True,
            )
            if converted.returncode:
                target.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
    time.sleep(1)
raise SystemExit(process.returncode)
