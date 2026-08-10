FROM mwader/static-ffmpeg:7.1 AS ffmpeg

FROM python:3.12-slim

# Static binary avoids pulling in Debian's ffmpeg package, which drags along
# ~450MB of GPU/TTS/SMT libraries (mesa, llvm, flite, z3) unrelated to the
# audio extraction this app actually does.
COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
