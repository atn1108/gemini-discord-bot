FROM node:22-bookworm-slim AS pot_builder
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --single-branch --branch 2.0.2 --depth 1 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil
WORKDIR /opt/bgutil/server
RUN npm ci --no-audit --no-fund && npx tsc && npm prune --omit=dev

FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=pot_builder /usr/local/bin/node /usr/local/bin/node
COPY --from=pot_builder /opt/bgutil/server /opt/bgutil-pot/server
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY discord_lifetime_memory_bot.py .
COPY gemini_wm.py .
COPY skills ./skills
CMD ["python", "discord_lifetime_memory_bot.py"]