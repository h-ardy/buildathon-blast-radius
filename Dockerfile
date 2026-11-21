# Blast Radius selector. The base image is the one the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
# the packages sqlglot's tests were labeled with (starter kit requirements.lock), from PyPI
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
COPY select.py runner.py /app/
COPY map /app/map
# pinned sqlglot sources and tests (from map/*.tar.gz), byte-compiled
RUN mkdir /app/sg && tar xzf /app/map/src.tar.gz -C /app/sg && tar xzf /app/map/fixtures.tar.gz -C /app/sg \
    && python -m compileall -q /app/sg && chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/select.py"]
