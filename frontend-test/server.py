#!/usr/bin/env python3
"""
TradeSystem Admin — Google OAuth 実接続確認用フロントサーバー
Python 3.6+ で動作。追加パッケージ不要。

起動:
  cd /home/opc/trade-system/frontend-test
  python3 server.py

アクセス:
  http://localhost:3000/login
"""
import http.server
import socketserver
import os

PORT = 3000
DIR = os.path.dirname(os.path.abspath(__file__))


class SPAHandler(http.server.SimpleHTTPRequestHandler):
    """全ルートに index.html を返す SPA ハンドラ"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def do_GET(self):
        # index.html 以外のパスへの直接アクセスもすべて index.html に転送（SPA ルーティング）
        self.path = "/index.html"
        return super().do_GET()

    def log_message(self, format, *args):
        # アクセスログを簡潔に出力
        print(f"  {args[0]} {args[1]}")


def main():
    print("=" * 50)
    print("TradeSystem Admin 接続確認フロント")
    print(f"URL: http://localhost:{PORT}/login")
    print("=" * 50)
    print()
    print("バックエンド API: http://localhost:8000")
    print("redirect_uri:    http://localhost:3000/auth/callback")
    print()
    print(".env の確認事項:")
    print("  OAUTH_REDIRECT_URI=http://localhost:3000/auth/callback")
    print("  ADMIN_FRONTEND_ORIGIN=http://localhost:3000")
    print("  COOKIE_SECURE=false")
    print("  GOOGLE_CLIENT_ID=<設定済みか確認>")
    print("  GOOGLE_CLIENT_SECRET=<設定済みか確認>")
    print("  TOTP_ENCRYPTION_KEY=<設定済みか確認>")
    print()
    print("バックエンド再起動が必要な場合:")
    print("  docker compose restart trade_app")
    print()
    print("Ctrl+C で停止")
    print()

    with socketserver.TCPServer(("", PORT), SPAHandler) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
