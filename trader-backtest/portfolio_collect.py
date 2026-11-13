#!/usr/bin/env python3
"""Explicit fresh pilot acquisition, separate from offline portfolio replay."""
import argparse
import datetime as dt
import hashlib
from pathlib import Path
import urllib.parse
import urllib.request

from collect import collect
from common import EPOCH, digest, read, write


def acquire(config, output):
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    frozen=output/"frozen-acquisition.json"
    protocol={"native_config":config,"native_config_sha256":digest(config),
        "fx":"EURUSD=X daily UTC bars; available 24 hours after start; maximum stale age four days",
        "note":"Fresh current-vendor snapshot, not reproduction of the missing expanded archive"}
    if frozen.exists() and read(frozen)!=protocol:
        raise ValueError("acquisition protocol differs; choose a new directory")
    write(frozen,protocol)
    collect(config,output/"data")
    begin=int((dt.datetime(2024,7,1)-EPOCH).total_seconds())
    finish=int((dt.datetime.fromisoformat(config["end"])+dt.timedelta(days=1)-EPOCH).total_seconds())
    url="https://query2.finance.yahoo.com/v8/finance/chart/EURUSD%3DX?"+urllib.parse.urlencode(
        {"interval":"1d","period1":begin,"period2":finish,"includePrePost":"false"})
    path=output/"fx"/"raw"/"EURUSD=X-1d.json"
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():
        req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req,timeout=30) as response:
            path.write_bytes(response.read())
    result=read(path)["chart"]["result"][0]
    if result["meta"].get("symbol")!="EURUSD=X" or result["meta"].get("dataGranularity")!="1d":
        raise ValueError("wrong FX archive identity or granularity")
    write(output/"fx"/"acquisition.json",{"url":url,"raw_sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
        "observations":len(result["timestamp"]),"quote_currency":"USD","base_currency":"EUR"})


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--native-config",default=str(Path(__file__).parent/"config.first.json"))
    p.add_argument("--output",required=True)
    a=p.parse_args()
    acquire(read(a.native_config),a.output)
