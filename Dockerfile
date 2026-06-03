FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY scraper/ ./scraper/
RUN pip install --no-cache-dir -e .

CMD ["scraper", "worker", "start"]
