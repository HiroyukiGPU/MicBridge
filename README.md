# MicBridge

MacとWindowsの間で、音声を家庭内LAN経由で送る小さなアプリです。次の両方向に対応します。

現在のバージョンは **2.0** です。受信側は初期版（v1.0）と前版（v1.1）の送信パケットも自動判別して受信できます。

- Macで再生している音楽をWindowsの音声入力にする
- WindowsにつないだマイクをMacの音声入力にする

## 必要なもの

- Windows 10/11とMacが同じLANに接続されていること
- 両方のPCにPython 3.11以降がインストールされていること
- システム再生音を取り込む側には仮想オーディオ機器が必要です

Mac用のBlackHoleは公式サイトから入手できます。

https://existential.audio/blackhole/

Windows用のVB-CABLEは公式サイトから入手できます。

https://vb-audio.com/Cable/

## Macの音楽をWindowsへ送る（今回の使い方）

1. この `MicBridge` フォルダーをWindowsとMacの両方へコピーします。
2. Macに `BlackHole 2ch`、Windowsに `VB-CABLE`をインストールします。
3. Macの「Audio MIDI設定」で、Macのスピーカー／ヘッドホンと`BlackHole 2ch`を含む「複数出力装置」を作ります。
4. Macのシステム音声出力を、その「複数出力装置」に変更します。
5. Windowsで `start-windows.bat` を起動し、「このPCで受ける」を選びます。
6. 音声デバイスに `CABLE Input (VB-Audio Virtual Cable)`、チャンネルに「ステレオ」、ポートに`50000`を指定して開始します。
7. Windows画面に表示されたIPアドレス（例：`192.168.1.30`）を控えます。
8. Macでターミナルを開き、`MicBridge`フォルダーへ移動して `zsh start-mac.command` を実行します。
9. Macで「このPCから送る」を選び、音声デバイスに`BlackHole 2ch`、チャンネルに「ステレオ」を選びます。
10. 手順7のWindowsのIPアドレスとポート`50000`を入力して開始します。
11. WindowsのZoom、Discord、OBSなどで、マイクとして `CABLE Output (VB-Audio Virtual Cable)`を選びます。

Mac側で音楽を聴く必要がなければ、「複数出力装置」の代わりにシステム出力を直接`BlackHole 2ch`へ変更しても構いません。

## WindowsのマイクをMacへ送る

1. Mac側で「このPCで受ける」を選び、出力を`BlackHole 2ch`にして開始します。
2. 表示されたMacのIPアドレスを控えます。
3. Windows側で「このPCから送る」を選び、マイクとMacのIPアドレスを指定して開始します。
4. Macの通話アプリなどで入力を`BlackHole 2ch`にします。

## 初回起動時

- Windows Defenderファイアウォールが確認を表示した場合は、「プライベートネットワーク」を許可します。
- macOSがターミナルやPythonの実行を止めた場合は、「システム設定」→「プライバシーとセキュリティ」から許可します。
- `start-mac.command`を開けない場合は、ターミナルでこのフォルダーへ移動し、`python3 micbridge.py`を実行します。

## 音が出ない場合

- 2台が同じルーターへ接続されているか確認します。ゲストWi-Fiでは端末間通信が遮断される場合があります。
- Windows側に入力レベルがあるか確認します。
- 送信側では仮想機器を「入力」、受信側では仮想機器を「出力」として選んでいるか確認します。
- アプリ側とMac側で同じポート番号を指定します。
- 両方のアプリで同じチャンネル設定を選びます。
- VPNを一時的に切り、もう一度試します。
- Windows DefenderファイアウォールでPythonまたはMicBridgeの「プライベートネットワーク」を許可します。
- 受信画面が「受信待機中」のままなら、パケットがWindowsへ届いていません。IPアドレス、ポート、ファイアウォールを確認します。
- 「UDPは到着しましたが形式が不正です」と表示された場合は、送信側のプロトコルが異なります。`SENDER_UPDATE_PROMPT.txt`を送信側の修正担当へ渡してください。

## 仕様と注意点

- 音声形式：48 kHz / 16-bit / monoまたはstereo
- 転送方式：UDP（モノラル約0.8 Mbps、ステレオ約1.6 Mbps）
- v2パケットは通常のLAN MTU内に収まる971 bytes以下です。
- 暗号化と認証はありません。信頼できる家庭内LANだけで使用してください。
- Macの音声をスピーカーから再生するとハウリングするため、ヘッドホンを推奨します。
