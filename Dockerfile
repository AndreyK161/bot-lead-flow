FROM python:3.12-slim AS runtime

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY apps ./apps
COPY scripts ./scripts

EXPOSE 8000

FROM runtime AS planner
CMD ["uvicorn", "apps.planner.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS bot
CMD ["uvicorn", "apps.bot.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
