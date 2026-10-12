FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY lostminer_bridge/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY lostminer_bridge/server.py /app/server.py
COPY lostminer_bridge/lostminer_native.py /app/lostminer_native.py
EXPOSE 25565/tcp
EXPOSE 25570/tcp
EXPOSE 4773/udp
CMD ["python3", "server.py", "--host", "0.0.0.0", "--mc-port", "25565", "--lostminer-port", "25570", "--lostminer-udp-port", "4773", "--world", "/app/world/world.json"]
