
FROM python:3.9

RUN mkdir /app
WORKDIR /app

RUN apt update
RUN apt-get update && apt-get install -y supervisor

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONPATH="/app"

# override this if you want to run it locally
# e.g. docker run -e "MATCHA_ENVIRONMENT=debug" -p 8002:8002 matcha:latest
ENV MATCHA_ENVIRONMENT="LIVE"

RUN mkdir -p /var/log/supervisor

COPY supervisord.conf supervisord.conf

EXPOSE 8002

CMD ["/usr/bin/supervisord"]