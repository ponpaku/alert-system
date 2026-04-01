# Nextcloud Talk 受付通知ボタン

Raspberry Pi 上で動作する、受付用の複数ボタン通知端末です。  
押したボタンごとに Nextcloud Talk へ異なるメッセージを送信します。

## 同梱ファイル

- `app.py` : 本体プログラム
- `config.toml.example` : 設定例
- `reception-alert.service` : systemd unit
- `setup.sh` : 導入スクリプト

## 主な仕様

- Nextcloud Talk へ専用ユーザー + アプリケーションパスワードで投稿
- ボタンごとにメッセージ変更
- 起動状態 LED
- 送信状態 LED
  - 送信中: 高速点滅
  - 成功後: 30 秒点灯
  - 失敗時: 30 秒ゆっくり点滅
- 連打抑止
- 再送制御
- `--test` と `--list-buttons` は GPIO を触らない

## メッセージ形式

通常送信:

```text
【受付通知】 人手をお願いします
場所：案内カウンターA
```

試験送信:

```text
【受付ボタン試験】 人手をお願いします
場所：案内カウンターA
```

## 想定 GPIO 割り当て

- 起動状態 LED: GPIO5  (物理 Pin 29)
- 送信状態 LED: GPIO27 (物理 Pin 13)
- ボタン1: GPIO17 (物理 Pin 11)
- ボタン2: GPIO22 (物理 Pin 15)
- ボタン3: GPIO23 (物理 Pin 16)
- GND: 物理 Pin 14 など

## 配線

### ボタン

各ボタンは **GPIO と GND の間** に接続します。

例:

- staff ボタン: GPIO17 - GND
- urgent ボタン: GPIO22 - GND
- emergency ボタン: GPIO23 - GND

スイッチに `COM / NO / NC` がある場合は **COM と NO** を使います。

### LED

各 LED は **GPIO → 330Ω → LED(+) → LED(-) → GND** です。

## 事前準備

Nextcloud 側で次を用意してください。

- 専用ユーザー
- そのユーザーのアプリケーションパスワード
- 通報先 Talk ルームのトークン
- 専用ユーザーをその Talk ルームへ参加させる

## 導入

### 1. ZIP を展開

```bash
unzip reception-alert-package.zip
cd reception-alert-package
```

### 2. セットアップ

```bash
sudo ./setup.sh
```

### 3. 設定編集

```bash
sudoedit /opt/reception-alert/config.toml
```

最低限、以下を設定してください。

- `nextcloud_base_url`
- `nextcloud_username`
- `nextcloud_app_password`
- `talk_room_token`
- `location_name`

### 4. ボタン一覧確認

```bash
sudo -u alert /usr/bin/python3 /opt/reception-alert/app.py /opt/reception-alert/config.toml --list-buttons
```

### 5. 試験送信

```bash
sudo -u alert /usr/bin/python3 /opt/reception-alert/app.py /opt/reception-alert/config.toml --test staff
```

### 6. サービス起動

```bash
sudo systemctl restart reception-alert
sudo systemctl status reception-alert
```

ログ確認:

```bash
journalctl -u reception-alert -f
```

## config.toml の例

`config.toml.example` を `config.toml` として使ってください。

### ボタンを増やす

`[[buttons]]` を増やせば追加できます。

```toml
[[buttons]]
name = "staff"
gpio = 17
prefix = "【受付通知】"
message = "人手をお願いします"
```

## 運用上の注意

- 本用途は現場ソリューションであり、法定の非常通報設備の代替ではありません。
- 有線 LAN を推奨します。
- ボタンは見ずに押す前提なら、形状・高さ・ガードで触感差をつける方が安全です。
- 誤操作を避けるため、ボタン数は 2〜3 個までを推奨します。
- ボタンは GPIO と GND のみを使い、3.3V / 5V へは接続しないでください。

## トラブルシュート

### `Permission denied: /opt/reception-alert/config.toml`

権限を確認してください。

```bash
sudo chown -R alert:alert /opt/reception-alert
sudo chmod 700 /opt/reception-alert
sudo chmod 600 /opt/reception-alert/config.toml
```

### `GPIO busy`

service が稼働中に別プロセスで GPIO を開こうとした可能性があります。  
このパッケージの `--list-buttons` と `--test` は GPIO を使わないので、通常は service と共存できます。

### 送信失敗

- URL 誤り
- room token 誤り
- 専用ユーザーが Talk ルーム未参加
- TLS 証明書エラー

を確認してください。
