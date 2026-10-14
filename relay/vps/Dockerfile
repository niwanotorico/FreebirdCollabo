# Freebird Collab relay (relay/collab_relay.py) as a container.
# Build context = repository root (docker-compose.yml does this):
#   docker build -f relay/vps/Dockerfile -t freebird-relay .
FROM python:3.12-slim
RUN useradd --system --no-create-home --shell /usr/sbin/nologin freebird
WORKDIR /app
# only the network modules of the add-on (no Blender code) + the relay
COPY freebird_collab/protocol.py freebird_collab/ws.py freebird_collab/hub.py \
     freebird_collab/traffic.py freebird_collab/link.py freebird_collab/
COPY relay/_netlib.py relay/collab_relay.py relay/
USER freebird
ENV PYTHONUNBUFFERED=1
EXPOSE 7788
HEALTHCHECK --interval=30s --timeout=5s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7788/', timeout=3)"
CMD ["python3", "relay/collab_relay.py", "--host", "0.0.0.0", "7788"]
