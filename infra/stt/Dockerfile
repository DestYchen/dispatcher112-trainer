FROM python:3.11-slim
RUN pip install --no-cache-dir vosk==0.3.45
COPY server.py /srv/server.py
EXPOSE 2700
CMD ["python", "/srv/server.py"]
