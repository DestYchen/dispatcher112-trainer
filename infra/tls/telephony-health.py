"""Require the actual verified TLS ARI endpoint, not only a running PBX process."""

import base64
import hashlib
import hmac
import os
import ssl
import urllib.request

password = hmac.new(
    os.environ["JWT_SECRET"].encode(), b"sip-control-v1", hashlib.sha256
).hexdigest()
authorization = base64.b64encode(f"dispatcher:{password}".encode()).decode()
request = urllib.request.Request(
    "https://telephony:8089/ari/asterisk/info",
    headers={"Authorization": f"Basic {authorization}"},
)
context = ssl.create_default_context(cafile="/run/credentials/tls/ca.crt")
context.minimum_version = ssl.TLSVersion.TLSv1_2
with urllib.request.urlopen(request, context=context, timeout=3) as response:
    assert response.status == 200
