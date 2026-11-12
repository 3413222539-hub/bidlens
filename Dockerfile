FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# pypdf 纯 Python，无需编译；先装依赖以利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 数据与档案挂载点，运行时由 volume 覆盖
RUN mkdir -p data/imports profiles

EXPOSE 8780

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8780/api/session', timeout=4).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "web_app:app", "--host", "0.0.0.0", "--port", "8780"]