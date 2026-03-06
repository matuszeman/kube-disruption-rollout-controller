FROM python:3.13-alpine AS prod-runtime

FROM prod-runtime AS dev-runtime
RUN apk add --no-cache \
    aws-cli # for aws sso login support

FROM dev-runtime AS dev
WORKDIR /app
COPY --from=app-src requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM prod-runtime AS prod
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY --from=app-src requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=app-src . .

USER nobody
CMD ["python", "main.py"]
