FROM alpine:3.24.2
RUN apk add --no-cache asterisk=22.9.0-r0 asterisk-srtp=22.9.0-r0 python3 espeak-ng sox openssl
COPY *.conf /etc/asterisk/
COPY run.py /opt/run.py
RUN addgroup -g 10001 dispatcher && adduser -D -u 10001 -G dispatcher dispatcher \
    && mkdir -p /run/asterisk /var/log/asterisk /var/spool/asterisk /var/cache/asterisk \
    && chgrp -R dispatcher /etc/asterisk && chmod -R g+rX /etc/asterisk \
    && chown -R dispatcher:dispatcher /run/asterisk /var/log/asterisk /var/spool/asterisk /var/cache/asterisk
USER 10001:10001
CMD ["python3", "/opt/run.py"]
