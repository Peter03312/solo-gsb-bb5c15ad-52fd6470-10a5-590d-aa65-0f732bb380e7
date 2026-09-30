FROM python:3.12-slim

# 不生成 .pyc、日志不缓冲
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# 先装依赖（利用层缓存）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用与测试
COPY app ./app
COPY tests ./tests

# 初始化自检：导入应用并运行核心算法测试，失败则构建失败
RUN python -c "from app.main import app; print('app import ok')" \
    && python -m unittest discover -s tests -v

EXPOSE 8080

# 容器内健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request,sys;\
port=os.getenv('API_PORT','8080');\
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3).status==200 else 1)"

# 由 Compose / 运行环境通过 API_HOST、API_PORT 配置
CMD ["sh", "-c", "python -m uvicorn app.main:app --host ${API_HOST:-0.0.0.0} --port ${API_PORT:-8080}"]
