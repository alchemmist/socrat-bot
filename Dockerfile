FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml alembic.ini ./
COPY alembic ./alembic
COPY src ./src

RUN pip install --no-cache-dir .

USER 65532

EXPOSE 8080

CMD ["python", "-m", "socrat_bot"]
