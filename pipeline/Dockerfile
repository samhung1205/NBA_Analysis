FROM python:3.11-slim

# nbainjuries 透過 jpype 呼叫 Java 解析官方 PDF 傷病報告，需要 JRE
RUN apt-get update \
  && apt-get install -y --no-install-recommends default-jre-headless libgomp1 \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x /app/docker-entrypoint.sh

ENTRYPOINT ["/app/docker-entrypoint.sh"]