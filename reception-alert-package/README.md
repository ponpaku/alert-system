# Reception Alert

Raspberry Pi 上で動作する、受付用の複数ボタン通知端末です。  
押したボタンを有界 FIFO キューへ積み、1 件ずつ複数の通知先へ順次配送します。

## 対応通知先

- Nextcloud Talk
- Nextcloud Bot
- Discord Webhook
- Slack Webhook
- LINE Bot
- Generic Webhook

## 主な仕様

- `Dispatcher` ベースの fan-out
- `DispatchResult` による成功 / 失敗 / 未着手の区別
- 未成功 destination のみ再試行
- `429` と `Retry-After` に対応
- shutdown 開始後は新規 enqueue しない
- shutdown 開始後は未着手 event に新規着手しない
- success は送信 LED を一定時間点灯
- failure / warning は送信 LED を点滅
- `--test` も本番と同じ配送基盤を通る

## 同梱ファイル

- `app.py` : エントリーポイント
- `alert_service.py` : GPIO / LED / queue / shutdown 制御
- `config.py` : config ローダーと fail-fast バリデーション
- `dispatcher.py` : 直列 fan-out と再試行制御
- `transport.py` : 共通 HTTP transport
- `destinations/` : 各通知先実装
- `config.toml.example` : 新 config 形式の設定例
- `reception-alert.service` : systemd unit
- `setup.sh` : 導入スクリプト

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

## config 形式

設定は `config.toml.example` の新形式が基準です。

主なセクション:

- `http`
- `gpio`
- `timing`
- `delivery`
- `destinations`
- `buttons`

`gpio.led_brightness` で alive/send 両 LED の明るさを `0 < 値 <= 1` の範囲で調整できます（例: `0.35`）。

### button ごとの送信先指定

`buttons[].destinations` を指定すると、その button は指定先にだけ送ります。  
未指定の場合は `enabled = true` の全 destination へ送ります。

### Generic Webhook テンプレート変数

以下を `{{ ... }}` 形式で使えます。

- `event_id`
- `button_name`
- `prefix`
- `message`
- `location_name`
- `kind`
- `occurred_at`
- `source`
- `text`

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

### 4. button 一覧確認

```bash
sudo -u alert /usr/bin/python3 /opt/reception-alert/app.py /opt/reception-alert/config.toml --list-buttons
```

### 5. 試験送信

```bash
sudo -u alert /usr/bin/python3 /opt/reception-alert/app.py /opt/reception-alert/config.toml --test staff
```

### 6. service 起動

```bash
sudo systemctl restart reception-alert
sudo systemctl status reception-alert
```

ログ確認:

```bash
journalctl -u reception-alert -f
```

## fail-fast バリデーション

起動時に次を検証します。

- `destination.name` 一意
- `button.name` 一意
- `button.gpio` 一意
- `buttons[].destinations` の未知名参照
- 同一 button 内の destination 重複
- Generic Webhook の `method`
- Generic Webhook の `content_type`
- Generic Webhook の `auth.type`
- Generic Webhook の payload 整合

## 運用メモ

- cooldown は押下時に判定します
- queue 満杯時は新規 event を破棄して warning ログを出します
- shutdown 開始後、未着手 event は破棄されます
- 本用途は現場ソリューションであり、法定の非常通報設備の代替ではありません

## テスト

```bash
python -m unittest discover -s tests -v
```
