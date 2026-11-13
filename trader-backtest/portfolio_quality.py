"""Apply the existing uniform event quality checks without selecting winners."""
import hashlib
from pathlib import Path

import numpy as np

from common import read, write
from data_quality import REASONS, classify

LABELS = ("VALID", "SUSPECT", "INVALID")


def audit(source, raw_folder, horizon, end, output):
    """QC is retrospective sensitivity, never a new event or entry rule.

    All events remain in ALL, including suspect/invalid outcomes when arithmetic
    is defined. VALID_ONLY is separately labelled, never fed back into ranking.
    """
    method = read(Path(__file__).parent / "event-methodology.json")
    days = source["days"]
    last = days.index(end)
    records, bars, reasons = {}, {}, {}
    for sym in source["universe"]:
        path = Path(raw_folder) / (sym + "-1d.json")
        if not path.exists():
            raise ValueError("source-quality raw archive missing: " + str(path))
        raw = read(path)["chart"]["result"][0]
        status, why, _, _ = classify(raw, days, method["quality"])
        bars[sym], reasons[sym] = status, why
        records[sym] = {"raw_sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
            "currency":raw.get("meta",{}).get("currency"),"instrument":raw.get("meta",{}).get("instrumentType"),
            "counts":{label:int(np.count_nonzero(status==i)) for i,label in enumerate(LABELS)}}
    for variant, events in source["events"].items():
        for event in events:
            i=days.index(event["day"])
            stop=min(last,i+horizon)
            window=bars[event["symbol"]][max(0,i-259):stop+1]
            why=reasons[event["symbol"]][max(0,i-259):stop+1]
            mask=int(np.bitwise_or.reduce(why)) if len(why) else 0
            event["quality"] = LABELS[int(np.max(window))] if len(window) else "INVALID"
            event["quality_reasons"] = [label for bit,label in REASONS.items() if bit & mask]
            event["quality_note"] = "Retrospective 260-session feature plus forward window; no entry filtering or price repair"
    value={"rule_sha256":hashlib.sha256((Path(__file__).parent/"event-methodology.json").read_bytes()).hexdigest(),
        "symbols":records,"flags":["NO_HISTORICAL_SECURITY_IDENTIFIER_ARCHIVE","NO_TRUE_POINT_IN_TIME_DATA"],
        "selection_policy":"No removal or replacement; primary ALL, retrospective VALID_ONLY forward sensitivity separate"}
    write(output,value)
    return value
