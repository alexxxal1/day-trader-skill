#!/usr/bin/env python3
"""Explicit acquisition and deterministic rebuild of the native daily/event layer."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
import urllib.request

from common import digest, module, read, write
from daily_collect import collect
from run import fingerprints


def freeze(path, value):
    path=Path(path)
    if path.exists() and digest(read(path))!=digest(value):
        raise ValueError("frozen protocol differs; choose another run directory: "+str(path))
    write(path,value)


def acquire(output):
    output=Path(output)
    source=output/"sources"
    source.mkdir(parents=True,exist_ok=True)
    scan=module("trader-scanner","scan")
    texts={}
    for name in scan.NASDAQ_FILES:
        path=source/name
        if not path.exists():
            request=urllib.request.Request(scan.NASDAQ_URL+name,headers={"User-Agent":scan.UA})
            with urllib.request.urlopen(request,timeout=30) as response:
                path.write_bytes(response.read())
        texts[name]=path.read_text()
    kept,counts,created=scan.build_universe(texts)
    classification={"kept":kept,"counts":counts,"created":created,
        "raw_sha256":{n:hashlib.sha256((source/n).read_bytes()).hexdigest() for n in texts},
        "historical_list_identity":"Original expanded-run list not present; reacquired identical native source/classifier. Count agreement does not prove original member identity."}
    freeze(source/"universe-classification.json",classification)
    collect(classification,output/"data",end="2026-11-06",workers=8)
    import exchange_calendars as xc
    if xc.__version__!="4.11.2":
        raise ValueError("calendar requires the predeclared exchange_calendars 4.11.2")
    schedule=xc.get_calendar("XNYS",start="1969-01-01",end="2026-11-06").schedule
    sessions={str(i.date()):{"open_utc":str(r["open"]).replace("+00:00","Z"),
                            "close_utc":str(r["close"]).replace("+00:00","Z")} for i,r in schedule.iterrows()}
    freeze(output/"data"/"calendar.json",{"source":"XNYS exchange_calendars 4.11.2","sessions":sessions})


def rebuild(output,workers=2):
    output=Path(output)
    data,analysis,events=output/"data",output/"analysis",output/"events"
    method=read(Path(__file__).parent/"event-methodology.json")
    freeze(events/"frozen-methodology.json",method)
    manifest=read(data/"acquisition.json")
    before=fingerprints()
    protocol={"acquisition_sha256":digest(manifest),"calendar_sha256":digest(read(data/"calendar.json")),
        "event_methodology_sha256":digest(method),"native_sources_sha256":before,
        "event_portfolio_config":read(Path(__file__).parent/"event-portfolio-config.json")}
    freeze(output/"frozen-rehydration.json",protocol)
    steps=[("daily_history.py",["all","--data",str(data),"--output",str(analysis),"--workers",str(workers)]),
        ("daily_verify.py",["--data",str(data),"--analysis",str(analysis),"--output",str(output/"native-equivalence.json")]),
        ("event_signals.py",["--analysis",str(analysis),"--output",str(events),"--workers",str(workers)]),
        ("data_quality.py",["--data",str(data),"--analysis",str(analysis),"--output",str(events)]),
        ("event_statistics.py",["--analysis",str(analysis),"--output",str(events)])]
    for name,args in steps:
        checkpoint=output/("completed-"+name+".json")
        key={"protocol_sha256":digest(protocol),"script_sha256":hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()}
        if checkpoint.exists():
            if read(checkpoint)!=key:
                raise ValueError("rebuild checkpoint does not match inputs")
            continue
        print("Rehydration stage",name,flush=True)
        subprocess.run([sys.executable,str(Path(__file__).parent/name)]+args,check=True)
        write(checkpoint,key)
    if fingerprints()!=before:
        raise RuntimeError("native strategies changed during rebuild")
    meta=read(analysis/"matrix-metadata.json")
    coverage=read(analysis/"ticker-coverage.json")
    stats=read(events/"event-statistics.json")
    write(output/"rehydration-audit.json",{"requested":len(meta["universe"]),
        "eligible":sum(r.get("common_eligible_sessions",0)>0 for r in coverage),
        "period":[meta["test_start"],meta["test_end"]],"event_counts":stats["event_counts"],
        "native_sources_unchanged":True,"original_list_membership_identity":"UNVERIFIABLE_ORIGINAL_MANIFEST_ABSENT"})


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("command",choices=("acquire","rebuild","all"))
    p.add_argument("--output",required=True)
    p.add_argument("--workers",type=int,default=2)
    a=p.parse_args()
    if a.command in ("acquire","all"):acquire(a.output)
    if a.command in ("rebuild","all"):rebuild(a.output,a.workers)
