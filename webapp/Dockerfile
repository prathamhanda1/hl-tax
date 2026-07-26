# Build context is the REPO ROOT (not webapp/), so this Dockerfile can copy
# both requirements files and the whole engine:
#
#     docker build -f webapp/Dockerfile -t hl-tax-engine .
#     docker run -p 8000:8000 hl-tax-engine
#
# Render/Railway both do this automatically when pointed at this file.

# ==============================================================================
# WHY: We need a lightweight but complete Python 3.11 environment to run the app.
# HOW: We use the official python:3.11-slim image which reduces the final image size.
# ==============================================================================
FROM python:3.11-slim

# ==============================================================================
# WHY: We need a dedicated directory inside the container to hold our application code.
# HOW: We set the working directory to /app. All subsequent commands execute here.
# ==============================================================================
WORKDIR /app

# ==============================================================================
# WHY: Docker layers are cached. By copying ONLY the dependency files first, 
#      we prevent Docker from reinstalling packages every time we change our python code.
#      We must maintain the folder structure so pip can find the second file.
# HOW: We use separate COPY commands. The first copies the root requirements to /app, 
#      and the second copies the webapp requirements into a /app/webapp/ directory.
# ==============================================================================
COPY requirements.txt ./
COPY webapp/requirements.txt ./webapp/

# ==============================================================================
# WHY: We need to install the Python dependencies required for the engine and webapp.
#      We use --no-cache-dir to keep the image size small by removing temporary pip caches.
# HOW: We run pip install, pointing to the exact paths of the files we just copied.
# ==============================================================================
RUN pip install --no-cache-dir -r requirements.txt -r webapp/requirements.txt

# ==============================================================================
# WHY: Now that dependencies are installed and cached, we bring in the actual source code.
#      This includes fx/data/*.csv files so a fresh container can price events immediately.
# HOW: We use COPY . . to copy everything from the build context (repo root) into /app.
# ==============================================================================
COPY . .

# ==============================================================================
# WHY: Cloud providers (like Render) assign dynamic ports at runtime via the PORT variable.
#      We need a default fallback in case the container is run locally.
# HOW: We set the ENV variable PORT to 8000 and EXPOSE it to document the expected port.
# ==============================================================================
ENV PORT=8000
EXPOSE 8000

# ==============================================================================
# WHY: The container needs a final execution command to start the API when it boots up.
# HOW: We use a shell command to run uvicorn, binding it to all network interfaces (0.0.0.0) 
#      and listening on the dynamically injected ${PORT} environment variable.
# ==============================================================================
CMD ["sh", "-c", "uvicorn webapp.server:app --host 0.0.0.0 --port ${PORT}"]
