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

# Deliberately after COPY app, and deliberately not in requirements.txt.
# YouTube breaks extraction constantly and yt-dlp ships fixes within days, so
# these two must not be pinned — but "not pinned" only means anything if the
# layer actually re-runs. Behind the requirements.txt copy it never did: that
# file changes maybe twice a year, so every `docker compose up --build`
# reinstalled from cache and the image kept serving whatever yt-dlp happened
# to be current the last time a dependency changed. Here, any edit to app/
# busts it, which on this project is every rebuild.
RUN pip install --no-cache-dir --upgrade yt-dlp bgutil-ytdlp-pot-provider

# yt-dlp caches its remote JS-challenge-solver component (see downloader.py's
# remote_components=["ejs:github"]) and PO tokens under XDG_CACHE_HOME.
# Pointed inside the already-persisted /app/data volume so it survives
# container recreates instead of re-downloading from GitHub on every
# `docker compose up --build`.
ENV XDG_CACHE_HOME=/app/data/.cache

EXPOSE 8000

# Do NOT add --workers (and don't set WEB_CONCURRENCY on this service either
# — uvicorn honors that env var exactly like --workers when the flag itself
# is omitted, which it is here). Download progress (app/services/backfill.py,
# app/services/bulk_import.py), app/progress.py's ProgressRegistry, and the
# recommendations build lock (app/services/recommendations.py) are all
# in-process, module-level state — a second uvicorn worker is a second OS
# process with its own copy of that state, so polling clients would see
# progress silently go missing or the build lock stop serializing runs.
# app/main.py's lifespan asserts WEB_CONCURRENCY isn't set to anything but 1
# for exactly this reason, but that can't catch an explicit `--workers N`
# added to this CMD line by hand — there's no reliable way for the app to
# detect that from inside the process (uvicorn spawns workers via
# multiprocessing's "spawn" context, and the child doesn't inherit the
# parent's sys.argv), so this comment is the only guard against that case.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
