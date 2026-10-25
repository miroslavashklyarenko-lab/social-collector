FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY collector ./collector
RUN useradd -m app && mkdir -p /app/data && chown -R app /app
USER app
VOLUME ["/app/data"]
ENTRYPOINT ["python", "-m", "collector"]
CMD ["run"]
