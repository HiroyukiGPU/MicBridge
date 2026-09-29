#!/bin/zsh
cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt
if [ $? -ne 0 ]; then
  echo "インストールに失敗しました。"
  read -k 1
  exit 1
fi
python3 micbridge.py
if [ $? -ne 0 ]; then
  echo "起動に失敗しました。"
  read -k 1
fi
