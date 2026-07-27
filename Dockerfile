FROM python:3.11-slim

# stdout/stderr'i tamponlamadan (unbuffered) yazdırır - hem docker-compose
# hem de Fly.io'nun `fly logs` komutu logları GERÇEK ZAMANLI göstersin diye
# (aksi halde Python bir dosyaya/pipe'a yazarken tam tamponlamaya geçer,
# loglar dakikalarca gecikmeli görünebilir - bkz. main.py'deki benzer
# stdout.reconfigure(line_buffering=True) notu, bu ENV o notla aynı amaca
# hizmet eder ama TÜM alt süreçler/kütüphaneler için de geçerlidir).
ENV PYTHONUNBUFFERED=1

# Çalışma dizini
WORKDIR /app

# Gereksinimleri kopyala ve kur
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Uygulama kodlarını ve klasörleri kopyala (.dockerignore ile .env ve data/
# HARİÇ TUTULUR - bkz. .dockerignore; bu, hem yerel docker-compose hem de
# Fly.io'nun uzak sunucusunda `fly deploy` ile yapılan build için GEÇERLİDİR,
# ikisi de aynı Dockerfile + .dockerignore çiftini kullanır).
COPY . .

# Uygulamanın çalışacağı web arayüzü portu (bkz. config.yaml > web.port ve
# fly.toml > http_service.internal_port - üçü de 8000 ile eşleşmeli).
EXPOSE 8000

# Tek komutla hem worker (periyodik tarama) hem Telegram bot dinleyicisi hem
# de web dashboard AYNI süreçte başlar (bkz. main.py) - Fly.io'da da TEK bir
# machine/process olarak bu şekilde çalışır, ayrı bir "worker process group"
# TANIMLANMAZ (bkz. fly.toml notları).
CMD ["python", "main.py"]
