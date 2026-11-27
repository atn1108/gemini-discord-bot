FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg nodejs \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY discord_lifetime_memory_bot.py .
COPY gemini_wm.py .
COPY skills ./skills
CMD ["python", "discord_lifetime_memory_bot.py"]