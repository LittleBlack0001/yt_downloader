# 1. 使用輕量級 Python 3.11 環境
FROM python:3.11-slim

# 2. 設定工作目錄
WORKDIR /app

# 3. 安裝系統套件：ffmpeg (影音合併) 與 nodejs (PO Token 生成)
RUN apt-get update && \
    apt-get install -y ffmpeg nodejs npm && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 4. 安裝 Python 套件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. 複製所有檔案
COPY . .

# 6. 建立暫存區並給予權限
RUN mkdir -p temp && chmod 777 temp

# 7. 啟動伺服器
CMD ["gunicorn", "--worker-class", "gthread", "--threads", "4", "--timeout", "600", "-b", "0.0.0.0:5000", "app:app"]