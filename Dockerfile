FROM python:3.12-slim
WORKDIR /app

# Bağımlılıklar (AI/BYOK için anthropic)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY web/ ./web/

# Public deploy'da kişisel veri KOPYALAMAYIN — boş şablondan başlatın.
COPY state.example.json ./state.json

ENV DASHBOARD_PORT=8787
ENV DASHBOARD_HOST=0.0.0.0
EXPOSE 8787

# Boş CSV + tracker.db kur (idempotent). Kalıcı veri için volume bağlayın: -v data:/app
RUN touch outreach_log.csv && python src/migrate.py || true

# Parolayı ve anahtarı deploy ortamında verin:
#   docker run -p 8787:8787 -e DASHBOARD_PASSWORD=... -e ANTHROPIC_API_KEY=... -v data:/app outreachos
CMD ["python", "src/app.py"]
