FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt  && playwright install --with-deps chromium
COPY app ./app
RUN useradd -r app && chown -R app /srv
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
