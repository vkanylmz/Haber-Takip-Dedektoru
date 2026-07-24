FROM python:3.11-slim

# Çalışma dizini
WORKDIR /app

# Gereksinimleri kopyala ve kur
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Uygulama kodlarını ve klasörleri kopyala
COPY . .

# Uygulamanın çalışacağı web arayüzü portu
EXPOSE 8000

# Tek komutla hem worker hem web dashboard başlar
CMD ["python", "main.py"]
