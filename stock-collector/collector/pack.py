# -*- coding: utf-8 -*-
"""
pack.py — 把 build/stocks 里按代码分好的小包整体打包，上传 R2 做长期归档
================================================================================
本地目录结构（collect_symbol.py 产出）：
  build/stocks/AAPL/AAPL.json
  build/stocks/TSLA/TSLA.json
  ...

打包规则：
  每 SHARD_SIZE 只股票打成一个 zip（默认 500 只 ≈ 170MB）
  输出：build/packs/stock-data-part-000.zip ...
  上传：R2 archive/stock-data-YYYYMMDD/part-000.zip

用法：
  python pack.py            # 只打包不传
  python pack.py --upload   # 打包并上传（需要 R2_ENABLE=1 和 R2_* 变量）
  python pack.py --split 200 --upload
================================================================================
"""
import os
import sys
import zipfile
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

BUILD_STOCKS = os.path.abspath(os.path.join(BASE, "..", "build", "stocks"))
PACK_DIR = os.path.abspath(os.path.join(BASE, "..", "build", "packs"))
SHARD_SIZE = 500


def main():
    upload = "--upload" in sys.argv
    size = SHARD_SIZE
    if "--split" in sys.argv:
        try:
            size = int(sys.argv[sys.argv.index("--split") + 1])
        except Exception:  # noqa: BLE001
            pass

    if not os.path.isdir(BUILD_STOCKS):
        print(f"没有可打包的数据目录: {BUILD_STOCKS}")
        return
    syms = sorted(d for d in os.listdir(BUILD_STOCKS)
                  if os.path.isdir(os.path.join(BUILD_STOCKS, d)))
    print(f"待打包 {len(syms)} 只股票，每包 {size} 只")
    os.makedirs(PACK_DIR, exist_ok=True)
    day = datetime.utcnow().strftime("%Y%m%d")
    packs = []

    for idx in range(0, len(syms), size):
        chunk = syms[idx:idx + size]
        part_no = idx // size
        out = os.path.join(PACK_DIR, f"stock-data-part-{part_no:03d}.zip")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for s in chunk:
                p = os.path.join(BUILD_STOCKS, s, f"{s}.json")
                if os.path.exists(p):
                    zf.write(p, arcname=f"stocks/{s}/{s}.json")
        mb = os.path.getsize(out) / 1048576
        print(f"  part-{part_no:03d}: {len(chunk)} 只, {mb:.1f} MB")
        packs.append((out, f"archive/stock-data-{day}/part-{part_no:03d}.zip"))

    if not upload:
        print("打包完成（未上传）。加 --upload 上传到 R2。")
        return

    from uploader import Uploader
    up = Uploader()
    if not up.r2:
        print("R2_ENABLE 未开启，跳过上传")
        return
    for path, key in packs:
        with open(path, "rb") as f:
            body = f.read()
        up.r2.put_object(Bucket=up.bucket, Key=key, Body=body,
                         ContentType="application/zip")
        print(f"  已上传 {key} ({len(body)//1048576} MB)")
    print(f"=== 归档完成：{len(packs)} 个包 -> archive/stock-data-{day}/ ===")


if __name__ == "__main__":
    main()
