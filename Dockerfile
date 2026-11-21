FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /app
COPY tools/requirements.lock /build/requirements.lock
RUN pip install --no-cache-dir -r /build/requirements.lock
COPY tools/fetch_baseline.py /build/fetch_baseline.py
RUN python /build/fetch_baseline.py /opt/sqlglot && chmod -R a+rX /opt/sqlglot
COPY selector.py verification.py probe.py /app/
COPY maps /app/maps
RUN chmod -R a+rX /app
USER 65534:65534
CMD ["python", "/app/selector.py"]
