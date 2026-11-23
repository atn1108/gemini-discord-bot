FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY discord_lifetime_memory_bot.py .
CMD ["python", "discord_lifetime_memory_bot.py"]