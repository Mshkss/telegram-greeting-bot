FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home bot

COPY --chown=bot:bot bot.py ./
COPY --chown=bot:bot app/ ./app/
USER bot

CMD ["python", "bot.py"]
