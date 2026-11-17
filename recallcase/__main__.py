"""Command line entry points for three business subsystems."""
import argparse,json,tempfile
from pathlib import Path
from .store import connect
from .adoption import adopt
from .assessment import assess
from .settlement import settle
from .report import report

def main():
    p=argparse.ArgumentParser()
    sub=p.add_subparsers(dest="command",required=True)
    i=sub.add_parser("init");i.add_argument("--db",required=True)
    for name in ("adopt","assess","settle"):
        cmd=sub.add_parser(name);cmd.add_argument("--db",required=True);cmd.add_argument("--input",required=True)
    r=sub.add_parser("report");r.add_argument("--db",required=True);r.add_argument("--out")
    d=sub.add_parser("demo");d.add_argument("--workdir")
    sl=sub.add_parser("stock-load");sl.add_argument("--db",required=True);sl.add_argument("--input",required=True)
    tr=sub.add_parser("transfer");tr.add_argument("--db",required=True);tr.add_argument("--id",required=True);tr.add_argument("--source",required=True);tr.add_argument("--from-distributor",required=True);tr.add_argument("--to-distributor",required=True);tr.add_argument("--lot",required=True);tr.add_argument("--units",required=True,type=int)
    a=p.parse_args()
    if a.command=="init":
        db=connect(a.db);db.close();print(a.db)
    elif a.command=="adopt":print(json.dumps(adopt(a.db,a.input),ensure_ascii=False))
    elif a.command=="assess":print(json.dumps(assess(a.db,a.input),ensure_ascii=False))
    elif a.command=="settle":print(json.dumps(settle(a.db,a.input),ensure_ascii=False))
    elif a.command=="report":print(json.dumps(report(a.db,a.out),ensure_ascii=False))
    elif a.command=="stock-load":
        from .stock import load_stock
        print(load_stock(a.db,**json.loads(Path(a.input).read_text(encoding='utf-8'))))
    elif a.command=="transfer":
        from .stock import transfer
        print(transfer(a.db,a.id,a.source,a.from_distributor,a.to_distributor,a.lot,a.units))
    else:
        work=Path(a.workdir or tempfile.mkdtemp(prefix="source-case-"));work.mkdir(parents=True,exist_ok=True)
        fixture=Path(__file__).resolve().parent.parent/"fixtures"
        db=work/"state.db"
        config=__import__(__package__+".policy",fromlist=["DEMO"]).DEMO
        print(adopt(db,fixture/config["adopt"]))
        print(assess(db,fixture/config["assess"]))
        print(settle(db,fixture/config["settle"]))
        print(work/"report.json")
        report(db,work/"report.json")
if __name__=="__main__":main()

