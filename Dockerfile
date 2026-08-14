FROM mwader/static-ffmpeg:7.1 AS ffmpeg

FROM python:3.12-slim

# Static binary avoids pulling in Debian's ffmpeg package, which drags along
# ~450MB of GPU/TTS/SMT libraries (mesa, llvm, flite, z3) unrelated to the
# audio extraction this app actually does.
COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg

WORKDIR /app

# yt-dlp needs a JS runtime to decipher YouTube's signature/n-param on the
# primary "web" player client; without one on PATH it silently falls back to
# non-JS clients (android_vr etc.) that YouTube intermittently 403s, causing
# some videos to fail to download while others succeed. deno is yt-dlp's
# default runtime if found on PATH — no extra config needed once it's here.
# Installed from the upstream zip release directly (via Python's stdlib)
# rather than apt (no `deno` package in Debian) or curl+unzip (not in the
# base image, and not worth adding just for this).
RUN python3 -c "\
import io, os, urllib.request, zipfile; \
data = urllib.request.urlopen('https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip').read(); \
zipfile.ZipFile(io.BytesIO(data)).extract('deno', '/usr/local/bin'); \
os.chmod('/usr/local/bin/deno', 0o755)"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# yt-dlp caches its remote JS-challenge-solver component (see downloader.py's
# remote_components=["ejs:github"]) and PO tokens under XDG_CACHE_HOME.
# Pointed inside the already-persisted /app/data volume so it survives
# container recreates instead of re-downloading from GitHub on every
# `docker compose up --build`.
ENV XDG_CACHE_HOME=/app/data/.cache

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
