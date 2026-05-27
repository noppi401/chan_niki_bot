# Ubuntu デプロイ手順（ニキ + niki-mcp + aivenv / Docker Compose）

trpg_terminal と同じ運用（`start-prod.sh` + 薄い systemd unit + `restart: always`）で
3 サービスを常駐させる。トンネルは従来どおり各プロセスが ngrok で管理する。

```
[Discord] ⇄ niki(bot, host net) ──→ niki-mcp(127.0.0.1:8000)  検索/天気/釣り
                       └──────────→ aivenv  (127.0.0.1:8080/8081) ホストDockerで実行
```

- **niki**: ngrok を 127.0.0.1 のローカルポートへ転送し、mcp/aivenv も localhost 前提で
  参照するため `network_mode: host`。bot 側のコード変更は不要。
- **niki-mcp / aivenv**: 通常のブリッジコンテナ。ポートは `127.0.0.1` のみに publish
  するので、外部到達は ngrok トンネル経由だけ（追加のファイアウォール開放は不要、SSH 22 のみ）。
- **aivenv**: ホストの Docker daemon を `/var/run/docker.sock` 経由で使い、サンドボックスを
  生成する。生成スクリプトの作業ディレクトリをホストと**同一パス**で共有する必要があるため、
  `AIVENV_WORK_DIR`(既定 `/opt/aivenv/work`) をボリュームと `TMPDIR` の双方に渡している。

## 1. 前提

```bash
sudo apt-get update
curl -fsSL https://get.docker.com | sudo sh    # Docker Engine + compose plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"                # 反映には再ログイン要
```
Docker Compose は **v2.17 以上**が必要（`additional_contexts` / `dockerfile_inline` を使用）。
`docker compose version` で確認。

## 2. リポジトリ配置

3 リポジトリを同じ親に並べる（既存の Windows 構成と同じ並び）。

```bash
sudo mkdir -p /opt/products && sudo chown "$USER" /opt/products
cd /opt/products
git clone <ニキ.git>            ニキ
git clone <fishing-mcp.git>     fishing-mcp
git clone <vm-play-server.git>  vm-play-server
```

## 3. 環境変数（.env は2つ）

```bash
cd /opt/products/ニキ
cp .env.example .env && nano .env                 # Discord/OpenAI/Google/ngrok
nano /opt/products/vm-play-server/.env            # OPENAI_API_KEY / NGROK_AUTHTOKEN
```

> **名前の差に注意**: ニキは `OPENAI_KEY`、aivenv は `OPENAI_API_KEY` を読む。

## 4. 起動

```bash
cd /opt/products/ニキ
bash start-prod.sh --build      # 初回はビルドあり。以後は引数なしで up -d
```

`start-prod.sh` は Docker 起動待ち → `AIVENV_WORK_DIR` を mkdir → `docker compose up -d` する。

## 5. boot 自動起動（任意）

各サービスは `restart: always` なので一度起動すれば再起動後も復帰するが、ホスト再起動後に
確実に立ち上げるなら systemd unit を入れる:

```bash
sudo cp deploy/systemd/niki-stack.service /etc/systemd/system/
# WorkingDirectory / ExecStart / ExecStop のパスを clone 先に合わせて編集
sudo nano /etc/systemd/system/niki-stack.service
sudo systemctl daemon-reload
sudo systemctl enable --now niki-stack.service
```

## 6. 確認

```bash
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f niki
curl -s 'http://127.0.0.1:8000/fishing/spots' | head    # mcp 疎通
curl -s 'http://127.0.0.1:8080/current'                  # aivenv 疎通
```

## 7. 更新（git pull 後）

```bash
cd /opt/products/ニキ && git pull
bash start-prod.sh --build
```

## 注意・トラブルシュート

- **ポート 8080 の衝突**: trpg_terminal の nginx も既定で host:8080 に publish する
  (`${HTTP_PORT:-8080}`)。同一ホストで併設するなら trpg 側 `.env` に `HTTP_PORT=8088`
  などを設定して衝突を避ける。
- **aivenv が docker を使えない**: コンテナは `/var/run/docker.sock` を root で使う。
  ホストの docker が動いているか (`systemctl status docker`)、ソケットが見えるかを確認。
  実行用イメージは既定で `python:3.11-slim`（ホスト daemon が初回 pull）。独自イメージを
  使う場合は `vm-play-server/.env` に `AIVENV_CONTAINER_IMAGE=` を設定し、その image を
  ホストで用意する。
- **セキュリティ**: aivenv はコンテナ内で 0.0.0.0 にバインドするが publish は
  `127.0.0.1` のみ。直接公開せず、必ず ngrok トンネル経由で外部に出すこと。
- **trpg 連携**: trpg を併設し URL 連携する場合は、docker-compose.prod.yml の niki
  サービスのコメント化された volume を有効にし、`.env` の `TRPG_NGROK_URL_FILE` と
  パスを合わせる。
