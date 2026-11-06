# -*- coding: utf-8 -*-
"""
uploader.py — 统一上传层：可直传自己服务器（PHP 接收端），可选同时写 R2
================================================================================
环境变量控制：
  INGEST_URL      例 https://你的域名/stock-ingest/ingest.php   （留空=不用服务器）
  INGEST_SECRET   与 PHP 里 SECRET 一致
  R2_ENABLE       1=同时写 R2（默认 0，暂停观察期建议 0）
  R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET

用法：
  up = Uploader()
  up.put("stocks/AAPL/edgar.json", json_bytes)
================================================================================
"""
import hashlib
import hmac
import json
import os
import time

import requests

_session = requests.Session()
_session.proxies.update({"http": None, "https": None})


class Uploader:
    def __init__(self):
        self.ingest_url = os.environ.get("INGEST_URL", "").strip()
        self.secret = os.environ.get("INGEST_SECRET", "").strip()
        self.r2 = None
        if os.environ.get("R2_ENABLE", "0") == "1":
            import boto3
            acc = os.environ["R2_ACCOUNT_ID"]
            self.r2 = boto3.client(
                "s3", endpoint_url=f"https://{acc}.r2.cloudflarestorage.com",
                aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                region_name="auto")
            self.bucket = os.environ["R2_BUCKET"]
        if not self.ingest_url and not self.r2:
            raise RuntimeError("未配置上传目标：请设置 INGEST_URL 或 R2_ENABLE=1")

    def put(self, path, body: bytes, retries=3):
        """path 如 stocks/AAPL/edgar.json；body 为 JSON bytes"""
        last = None
        for i in range(retries):
            try:
                if self.ingest_url:
                    self._post(self.ingest_url, path, body)
                if self.r2:
                    self.r2.put_object(Bucket=self.bucket, Key=path, Body=body,
                                       ContentType="application/json",
                                       CacheControl="max-age=600")
                return True
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(2 * (i + 1))
        raise RuntimeError(f"upload failed {path}: {last}")

    def _post(self, url, path, body):
        sign = hmac.new(self.secret.encode(), f"{path}|".encode() + body,
                        hashlib.sha256).hexdigest()
        r = _session.post(url, params={"path": path}, data=body,
                          headers={"X-Sign": sign,
                                   "Content-Type": "application/octet-stream"},
                          timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
