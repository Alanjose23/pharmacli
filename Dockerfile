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
# the document list, and only that: the PDFs, the SQLite panel and the promoted
# model are mounted at run time, so no data and no secret is baked in
WORKDIR /app
COPY corpus.json ./
ENV PATH=/venv/bin:$PATH PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 1000 app && mkdir -p /app/data && chown -R app /app
USER app
EXPOSE 8000
# one worker: the panel is ~1GB in memory and Ollama is the bottleneck anyway,
# so a second worker would double the RAM to queue on the same GPU-less box
CMD ["uvicorn", "pharmacli.serve:app", "--host", "0.0.0.0", "--port", "8000"]
