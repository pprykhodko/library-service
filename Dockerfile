FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install -r requirements.txt

COPY . .
RUN python -m pip install --no-deps -e . \
    && groupadd --gid 10001 library \
    && useradd --uid 10001 --gid library --create-home library \
    && mkdir -p /data /beat \
    && chown -R library:library /app /data /beat

USER library

EXPOSE 8000

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000", "--noreload"]
