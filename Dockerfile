FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY events.json .
RUN useradd --create-home bot && mkdir /app/data && chown -R bot:bot /app
USER bot
EXPOSE 8000
CMD ["python", "-m", "app"]
