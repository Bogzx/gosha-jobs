import json, sys, importlib.util
spec=importlib.util.spec_from_file_location("f_old", sys.argv[1]); old=importlib.util.module_from_spec(spec); spec.loader.exec_module(old)
import gosha.filters as new
from gosha.evaluation import APPLICABLE_LEVELS
P=[json.loads(l) for l in open("eval/postings.jsonl")]
R=[json.loads(l) for l in open("eval/personas.jsonl")]
tot={}
for variant in ("{k}", "junior {k}", "{k} intern"):
    o=n=g=so=sn=s=0
    for r in R:
        kw=variant.format(k=r["keyword"])
        for p in P:
            if r["family"] not in p["families"]: continue
            if p["level"] in APPLICABLE_LEVELS:
                g+=1; o+=old.title_is_relevant(p["title"],kw); n+=new.title_is_relevant(p["title"],kw,None,p["description"])
            elif p["level"]=="senior":
                s+=1; so+=old.title_is_relevant(p["title"],kw); sn+=new.title_is_relevant(p["title"],kw,None,p["description"])
    print(f"{variant:14} grade-2 passing gate: old {o}/{g}  new {n}/{g}   | same-family senior passing: old {so}/{s} new {sn}/{s}")
