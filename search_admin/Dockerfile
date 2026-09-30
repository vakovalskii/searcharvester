# search-admin: settings page backend for SearXNG (see server.py). Small on purpose:
# it holds the Docker socket, so it has no dependencies beyond YAML and a proxy test.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY settings.py server.py ./
COPY tests ./tests
EXPOSE 8011
HEALTHCHECK --interval=5s --timeout=3s --retries=20 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8011/health', timeout=2)"
CMD ["python", "server.py"]
