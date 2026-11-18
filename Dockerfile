# Two stages so the compiler toolchain pip may need stays out of the image that
# ships: the builder produces /venv, the runtime copies it and nothing else.
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
# analysis for /predict (pandas, sklearn), serve for FastAPI, rag for PyMuPDF.
# Not editable: the runtime stage has no /src, only the built package.
RUN python -m venv /venv \
 && /venv/bin/pip install --no-cache-dir ".[analysis,serve,rag]"

FROM python:3.12-slim
COPY --from=build /venv /venv
# the document list and the 2.1MB demo fixture. The real PDFs, the full SQLite
# panel and the promoted model are mounted at run time, so no shelf data, no
# acquisition costs and no secret are baked in
WORKDIR /app
# classes.json: how labels name each drug's class, read by the pair checks.
# Reference data like corpus.json, with nothing about a patient in it.
COPY corpus.json classes.json ./
# the demo panel and the model trained on it (tests/make_demo.py, 2.1MB): what
# makes /predict answer on a clean clone. The real panel and promoted model are
# still mounted, never baked in.
COPY data/demo.db ./data/
COPY models/demo-mover.joblib models/demo-mover.json ./models/
# the image points at the fixture it ships, so `docker run` with no mounts and
# no compose still answers /predict. Anything passed in wins: compose sets these
# from .env, and a mounted real panel is one override away.
ENV PATH=/venv/bin:$PATH PYTHONUNBUFFERED=1 \
    PHARMACLI_DB=data/demo.db \
    PHARMACLI_MODEL=models/demo-mover.joblib
# /app/patients is where compose mounts the patient-data volume; it exists and
# belongs to `app` here so the volume is writable by the non-root user
RUN useradd --create-home --uid 1000 app && mkdir -p /app/data /app/patients \
 && chown -R app /app
USER app
EXPOSE 8000
# one worker: the panel is ~1GB in memory and Ollama is the bottleneck anyway,
# so a second worker would double the RAM to queue on the same GPU-less box
CMD ["uvicorn", "pharmacli.serve:app", "--host", "0.0.0.0", "--port", "8000"]
