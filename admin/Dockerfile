FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY app.py .
COPY templates/ ./templates/
COPY static/ ./static/

EXPOSE 7861
CMD ["python", "app.py"]
