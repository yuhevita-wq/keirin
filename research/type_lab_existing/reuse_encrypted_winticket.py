#!/usr/bin/env python3
"""Consume EXISTING encrypted WINTICKET daily SQLite Releases only; NO web collection.

Read source data with owning COLLECTOR_PASSPHRASE supplied via GitHub Actions
secret. Re-encrypt small race-wise candidate A-F 'arare' inputs using the
market-analysis public CMS certificate for a secure join with historical PL p3.
All original p3 from the independent trained model, not source new estimates.
This is read-only relative to live collection, branch and release assets.
"""
from __future__ import annotations
import argparse, collections, datetime as dt, gzip, hashlib, json, os, calendar
import sqlite3, subprocess, tempfile
from pathlib import Path

CERT=Path(__file__).parent/"market_analysis_public_cert.pem"
BEHIND_THRESHOLD=11.0
DATES=("2023-10-19","2024-10-20","2025-03-21")
REQUIRED_RACE={"race_key","venue_id","race_date","race_no","day_index","n_entries"}
REQUIRED_ENTRY={"race_key","frame_no","player_id","name","prefecture","term",
                "style","race_point","ex_left_behind_pct","line_group","line_pos"}

def _run(cmd,**kw):
    return subprocess.run(cmd,check=False,stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE,timeout=100,**kw)

class Seal:
    def __init__(self,path):self.path=Path(path);self.count=0
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.proc=subprocess.Popen(["openssl","cms","-encrypt","-binary",
            "-aes-256-gcm","-stream","-outform","DER","-out",str(self.path),str(CERT)],
            stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        self.gz=gzip.GzipFile(fileobj=self.proc.stdin,mode="wb",mtime=0)
        return self
    def write(self,obj):
        self.gz.write((json.dumps(obj,ensure_ascii=False,separators=(",",":"))+"\n").encode())
        self.count+=1
    def __exit__(self,err,_,__):
        self.gz.close();self.proc.stdin.close()
        rc=self.proc.wait(timeout=120)
        if err or rc:
            self.path.unlink(missing_ok=True)
            if not err:raise RuntimeError("CMS output encryption failed")
        return False

def score_for_axis(axis,rows,day_index):
    """Mirror original kiseki.type_lab.race_shape arare score for a candidate top-p3 car."""
    obj=rows.get(axis)
    if obj is None:return {"arare":None,"block":"NO_AXIS_RIDER"}
    group=obj["line_group"]
    if group is None or str(group) in ("","0"):
        members=[];size=1
    else:
        members=[int(c) for c,e in rows.items() if e["line_group"]==group]
        size=len(members)
    score=1 if size==2 else (-1 if size>=4 else 0)
    if members:
        leads=[c for c in members if rows[c]["line_pos"]==1]
        seconds=[c for c in members if rows[c]["line_pos"]==2]
        if len(leads)!=1:return {"arare":None,"block":"LINE_LEADER_MISSING_OR_AMBIGUOUS"}
        lead=rows[leads[0]]
        behind=lead["ex_left_behind_pct"]
        if behind is None:return {"arare":None,"block":"LEADER_EX_LEFT_BEHIND_MISSING"}
        score+=-1 if behind>=BEHIND_THRESHOLD else 1
        score+=2 if lead["style"]=="追" else 0
    else:
        leads=[];seconds=[]
    if day_index is None:return {"arare":None,"block":"DAY_INDEX_MISSING"}
    score+=int(day_index)-2
    if leads and seconds:
        lead=rows[leads[0]];second=rows[seconds[0]]
        if lead["race_point"] is None or second["race_point"] is None:
            return {"arare":None,"block":"LEADER_OR_SECOND_RACE_POINT_MISSING"}
        score+=int(second["race_point"]>lead["race_point"])
    return {"arare":score,"block":None}

def tablecolumns(con,name):
    return {row[1] for row in con.execute(f"PRAGMA table_info({name})")}

def parse_db(file):
    db=sqlite3.connect(f"file:{file}?mode=ro",uri=True)
    db.row_factory=sqlite3.Row
    try:
        if db.execute("PRAGMA quick_check").fetchone()[0]!="ok":
            raise RuntimeError("SQLITE_QUICK_CHECK_ERROR")
        if not REQUIRED_RACE<=tablecolumns(db,"wt_races") or not REQUIRED_ENTRY<=tablecolumns(db,"wt_entries"):
            raise RuntimeError("MISSING_CRITICAL_WINTICKET_COLUMNS")
        out=[];count=collections.Counter(); reasons=collections.Counter()
        for rr in db.execute("SELECT * FROM wt_races ORDER BY race_key"):
            race=dict(rr);rid=str(race["race_key"])
            count["races"]+=1
            raw=[dict(e) for e in db.execute("SELECT * FROM wt_entries WHERE race_key=? ORDER BY frame_no",(rid,))]
            active={int(x["frame_no"]):x for x in raw if x["frame_no"] is not None}
            size=len(active)
            if size not in (7,9) or size!=len(raw) or set(active)!=set(range(1,size+1)):
                count["invalid_7_9_roster"]+=1
                continue
            if race.get("n_entries") and int(race["n_entries"])!=size:
                count["advertised_roster_disagrees"]+=1
            for e in active.values():
                for col in REQUIRED_ENTRY-{"race_key","frame_no"}:
                    count["filled_"+col]+=int(e.get(col) is not None and str(e.get(col))!="")
            vals={}
            for car in range(1,size+1):
                vals[str(car)]=score_for_axis(car,active,race.get("day_index"))
                if vals[str(car)]["block"]:
                    reasons[vals[str(car)]["block"]]+=1
            exact=(all(v["arare"] is not None for v in vals.values()))
            count["complete_7_9_races"]+=1
            count["all_axis_arare_available"]+=int(exact)
            out.append({"race_key":rid,"race_date":str(race["race_date"]),
                        "venue_id":str(race["venue_id"]).zfill(2),
                        "race_no":int(race["race_no"]),
                        "cup_id":race.get("cup_id"),
                        "day_index":race.get("day_index"),
                        "n_cars":size,"possible_axis_arare":vals,
                        "source":"original_winticket_encrypted_daily_sqlite",
                        "retrospective_archived_record":True,
                        "is_original_type_lab_live_snapshot":False,
                        "all_car_line_positions":{str(k):int(v["line_pos"]) if v.get("line_pos") is not None else None for k,v in active.items()},
                        "official_player_ids":{str(k):v.get("player_id") for k,v in active.items()},
                        "race_points":{str(k):v.get("race_point") for k,v in active.items()},
                        "ex_left_behind_pct":{str(k):v.get("ex_left_behind_pct") for k,v in active.items()},
                       })
        count["total_entry_rows"]=db.execute("SELECT COUNT(*) FROM wt_entries").fetchone()[0]
        return out,count,reasons
    finally:db.close()

def run(targets,out,allow_missing=False):
    passphrase=os.environ.pop("COLLECTOR_PASSPHRASE","")
    if len(passphrase)<32:raise RuntimeError("COLLECTOR_PASSPHRASE_UNAVAILABLE")
    out.mkdir(parents=True,exist_ok=True)
    counts=collections.Counter();details=[];failures={};why=collections.Counter()
    with tempfile.TemporaryDirectory(prefix="type_lab_reuse_gpg_") as temp:
        base=Path(temp)
        sealed=out/"existing_winticket_arare_by_axis.jsonl.gz.cms"
        with Seal(sealed) as writer:
            for date in targets:
                parsed=dt.date.fromisoformat(date)
                tag=f"training-data-{parsed:%Y-%m}"
                name=f"winticket-{date}.sqlite.gpg"
                folder=base/date
                folder.mkdir()
                q=_run(["gh","release","download",tag,"--pattern",name,
                       "--dir",str(folder)])
                blob=folder/name
                if q.returncode!=0 or not blob.is_file() or blob.stat().st_size==0:
                    failures[date]="RELEASE_ASSET_UNAVAILABLE"
                    continue
                dbpath=folder/"read.sqlite"
                with dbpath.open("wb") as plain:
                    proc=subprocess.run(["gpg","--batch","--quiet","--pinentry-mode","loopback",
                           "--passphrase-fd","0","--decrypt",str(blob)],
                           input=passphrase.encode("utf8")+b"\n",
                           stdout=plain,stderr=subprocess.DEVNULL,timeout=180)
                if proc.returncode!=0:
                    failures[date]="GPG_DECRYPT_FAILED_OR_WRONG_SECRET"
                    blob.unlink(missing_ok=True);dbpath.unlink(missing_ok=True)
                    continue
                try:
                    records,c,r=parse_db(dbpath)
                except Exception as e:
                    failures[date]="SQLITE_SCHEMA_OR_READ_ERROR_"+type(e).__name__
                    records=[];c=collections.Counter();r=collections.Counter()
                finally:
                    dbpath.unlink(missing_ok=True)
                    blob.unlink(missing_ok=True)
                for record in records:
                    if record["race_date"]!=date:
                        raise RuntimeError("NONMATCHING_ARCHIVE_DATE")
                    writer.write(record)
                counts.update(c)
                why.update(r)
                details.append({"date":date,"races":c["races"],"complete_7_9":c["complete_7_9_races"],
                    "all_axis_arare":c["all_axis_arare_available"],"entry_rows":c["total_entry_rows"],
                    "missing_reasons":dict(r)})
                print("TYPE_LAB_EXISTING_DAY",json.dumps(details[-1],ensure_ascii=False),flush=True)
    report={"schema":"type_lab_existing_winticket_no_recollection_v1",
            "requested_days":list(targets),"requested_count":len(targets),
            "opened_existing_days":len(details),"release_or_decrypt_failures":failures,
            "counts":dict(counts),"missing_input_reasons":dict(why),
            "by_day":details,"encrypted_records":sealed.name,
            "encrypted_sha256":hashlib.sha256(sealed.read_bytes()).hexdigest(),
            "encrypted_race_records":writer.count,
            "collector_new_http_requests":0,
            "source_live_backfill_unmodified":True,
            "historical_racecard_values_are_not_original_live_typelab_snapshots":True}
    (out/"existing_winticket_reuse_report.json").write_text(
        json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf8")
    print("TYPE_LAB_EXISTING_REUSE_DONE",json.dumps({
      "requested_days":len(targets),"opened_days":len(details),
      "verified_races":counts["races"],"full_7_9":counts["complete_7_9_races"],
      "arare_complete":counts["all_axis_arare_available"],"encrypted_records":writer.count,
      "failures":len(failures)},ensure_ascii=False),flush=True)
    if writer.count==0 and not allow_missing:raise RuntimeError("NO_HISTORICAL_SAVED_WINTICKET_RACES")
    if failures and not allow_missing:raise RuntimeError("SOME_EXISTING_RELEASE_DAYS_UNAVAILABLE")

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--dates",default=",".join(DATES))
    parser.add_argument("--month",help="YYYY-MM. Read this month ONLY from stored Release assets.")
    parser.add_argument("--allow-missing",action="store_true",help="Keep separate, explicit missing asset report.")
    parser.add_argument("--output",default="research-output")
    args=parser.parse_args()
    if args.month:
        ym=dt.date.fromisoformat(args.month+"-01")
        end=calendar.monthrange(ym.year,ym.month)[1]
        if args.month=="2025-04":end=min(end,19)
        days=[f"{args.month}-{k:02d}" for k in range(1,end+1)]
    else:days=[x.strip() for x in args.dates.split(",") if x.strip()]
    run(days,Path(args.output),args.allow_missing)
