FROM python:3.11-slim

# タイムゾーンをJSTに設定（日本株取引のため必須）
ENV TZ=Asia/Tokyo
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# システム依存ライブラリ
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Pythonパッケージをインストール（レイヤーキャッシュ活用）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションコードをコピー
COPY . .

# 非rootユーザーで実行（セキュリティ）
RUN useradd -m -u 1000 trader && chown -R trader:trader /app
USER trader

# ログディレクトリ作成
RUN mkdir -p /app/logs

# DB マイグレーション → アプリ起動
CMD ["sh", "-c", "alembic upgrade head && uvicorn trade_app.main:app --host 0.0.0.0 --port 8000 --workers 2"]
