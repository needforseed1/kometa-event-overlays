FROM python:3.14-alpine

COPY requirements.txt /tmp/requirements.txt
RUN adduser -D -H -u 1000 -G users kometa \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && mkdir -p /data \
    && chown 1000:100 /data \
    && rm /tmp/requirements.txt

WORKDIR /app
COPY imdb_episode_ratings.py /app/imdb_episode_ratings.py
COPY event_daemon.py /app/event_daemon.py

USER 1000:100
ENTRYPOINT ["python", "-u", "/app/event_daemon.py"]
CMD ["--event-daemon"]
