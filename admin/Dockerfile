FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY app.py .
COPY templates/ ./templates/
COPY static/ ./static/

EXPOSE 7861
CMD ["gunicorn", "--workers=1", "--threads=8", "--timeout=60", "--graceful-timeout=30", "--access-logfile=-", "--error-logfile=-", "--bind=0.0.0.0:7861", "app:app"]
