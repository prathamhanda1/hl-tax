# Phase 8F — container image for a free/cheap host (Render, Railway, Fly).
#
# Build context is the REPO ROOT (not webapp/), so this Dockerfile can copy
# both requirements files and the whole engine:
#
#     docker build -f webapp/Dockerfile -t hl-tax-engine .
#     docker run -p 8000:8000 hl-tax-engine
#
# Render/Railway both do this automatically when pointed at this file.

FROM python:3.11-slim

WORKDIR /app

# Install dependencies first so this layer is cached across code edits.
COPY requirements.txt webapp/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r webapp/requirements.txt

# Then the code. fx/data/*.csv (the FBIL rate series) ships inside the image
# so a fresh container can price events immediately, without a network call
# on first boot.
COPY . .

# $PORT is set by the hosting platform at runtime; 8000 is the local fallback.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn webapp.server:app --host 0.0.0.0 --port ${PORT}"]
