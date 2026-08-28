FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p logs

# Bake static assets (incl. drf-spectacular-sidecar's Swagger UI/Redoc JS/CSS)
# into the image at build time -- airgapped means no internet AND no live
# .env available here, so DJANGO_SECRET_KEY is a build-only placeholder,
# overridden by the real one from env_file at `docker compose up` time.
ENV DJANGO_SECRET_KEY=build-time-placeholder-overridden-at-runtime
RUN python manage.py collectstatic --noinput

EXPOSE 8001
CMD ["python", "serve.py"]
