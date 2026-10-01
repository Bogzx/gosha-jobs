"""Run the offline ranking eval (gosha/evaluation.py) and report the numbers.

    # the production model (SEMANTIC_MODEL), results as JSON
    python scripts/eval_ranking.py --write-results eval/results

    # compare models; "@512" overrides a model's token window
    python scripts/eval_ranking.py --models tfidf all-mpnet-base-v2 \
        paraphrase-multilingual-mpnet-base-v2 paraphrase-multilingual-mpnet-base-v2@512

    # regression gate (CI): fail if the production model got worse
    python scripts/eval_ranking.py --check eval/results/all-mpnet-base-v2.json

Needs sentence-transformers for every model except `tfidf`. See docs/EVAL.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

# The matrices here are tiny; a multi-threaded BLAS only fights other
# processes for cores (on a busy 4-core box: minutes instead of seconds).
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from gosha import evaluation  # noqa: E402
from gosha.matching import DEFAULT_MODEL, build_job_text  # noqa: E402


def slug(name: str) -> str:
    return name.replace("/", "_").replace("@", "_seq")


def cached(encode, cache_file: Path):
    """Wrap `encode` with an on-disk cache keyed by text hash.

    Encoding ~250 texts takes minutes on a busy CPU; metrics and variants
    are iterated on far more often than the model changes.
    """
    store: dict[str, np.ndarray] = {}
    if cache_file.exists():
        with np.load(cache_file) as data:
            store = {key: data[key] for key in data.files}

    def wrapped(texts: list[str]):
        keys = [hashlib.sha1(t.encode("utf-8")).hexdigest() for t in texts]
        missing = [i for i, k in enumerate(keys) if k not in store]
        if missing:
            fresh = encode([texts[i] for i in missing])
            for i, vec in zip(missing, fresh):
                store[keys[i]] = np.asarray(vec, dtype=np.float32)
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            np.savez(cache_file, **store)
        return np.stack([store[k] for k in keys])

    return wrapped


def make_encoder(spec: str, postings, personas, cache_dir: Path | None = None):
    """(encode, info) for a model spec: 'tfidf', 'name' or 'name@<max_seq_length>'."""
    if spec == "tfidf":
        corpus = [build_job_text(p.title, p.company, p.description) for p in postings]
        corpus += [u.cv for u in personas]
        return evaluation.tfidf_encoder(corpus), {"max_seq_length": None}

    import sklearn  # noqa: F401  (aarch64: load sklearn's libgomp before torch)
    from sentence_transformers import SentenceTransformer

    name, _, seq = spec.partition("@")
    model = SentenceTransformer(name)
    if seq:
        model.max_seq_length = int(seq)

    def encode(texts: list[str]):
        return model.encode(
            texts, convert_to_numpy=True, normalize_embeddings=True, batch_size=32,
        )

    if cache_dir is not None:
        encode = cached(encode, cache_dir / f"{slug(spec)}.npz")
    return encode, {"max_seq_length": model.max_seq_length}


def _sweep_row(sweep: list[dict], threshold: float) -> dict:
    return next(r for r in sweep if abs(r["threshold"] - threshold) < 1e-9)


def summary_markdown(results: dict[str, dict]) -> str:
    lines = ["| Model | window | nDCG@10 (95% CI) | R-prec | MRR | P@10 | nDCG@10 EN postings | "
             "nDCG@10 RO postings | nDCG@10 EN CVs | nDCG@10 RO CVs |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for name, r in results.items():
        f = r["feed"]
        lo, hi = f["ndcg@10_ci95"]
        by_post, by_cv = f["by_posting_lang"], f["by_cv_lang"]
        lines.append(
            f"| `{name}` | {r['max_seq_length'] or '–'} | {f['mean']['ndcg@10']:.3f} "
            f"({lo:.3f}–{hi:.3f}) | {f['mean']['r_precision']:.3f} | {f['mean']['mrr']:.3f} | "
            f"{f['mean']['p@10']:.3f} | {by_post.get('en', {}).get('ndcg@10', 0):.3f} | "
            f"{by_post.get('ro', {}).get('ndcg@10', 0):.3f} | "
            f"{by_cv.get('en', {}).get('ndcg@10', 0):.3f} | {by_cv.get('ro', {}).get('ndcg@10', 0):.3f} |"
        )
    for name, r in results.items():
        if r["model"] == "tfidf":
            continue  # TF-IDF cosines live on another scale: thresholds are meaningless
        for path, label in (("dm_with_cv", "with a CV"), ("dm_search_only", "search only")):
            lines += ["", f"Discord delivery, `{name}`, {label}:", "",
                      "| threshold | recall (grade 2) | off-target passed (grade 0) | "
                      "precision (grade 2) | precision (grade ≥1) | passed per persona |",
                      "|---|---|---|---|---|---|"]
            for row in r[path]["sweep"]:
                lines.append(
                    f"| {row['threshold']:.2f} | {row['recall']:.3f} | "
                    f"{row['off_target_pass_rate']:.3f} | {row['precision']:.3f} | "
                    f"{row['precision_any']:.3f} | {row['per_persona_passed']:.1f} |")
        if "feedback" in r:
            lines += ["", f"Rocchio feedback, `{name}` (residual nDCG@10, Δ = after − before):", "",
                      "| like w | dislike w | rounds | before | after | Δ 95% CI |",
                      "|---|---|---|---|---|---|"]
            for row in r["feedback"]:
                if not row["personas"]:
                    continue
                lo, hi = row["delta_ci95"]
                lines.append(
                    f"| {row['like_weight']} | {row['dislike_weight']} | {row['rounds']} | "
                    f"{row['ndcg@10_before']:.3f} | {row['ndcg@10_after']:.3f} | "
                    f"{lo:+.3f} … {hi:+.3f} |")
    return "\n".join(lines) + "\n"


def check(result: dict, baseline_path: Path, tolerance: float) -> list[str]:
    """Regressions of `result` against a committed baseline (empty = pass)."""
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    problems = []
    now, then = result["feed"]["mean"]["ndcg@10"], baseline["feed"]["mean"]["ndcg@10"]
    if now < then - tolerance:
        problems.append(f"feed nDCG@10 {now:.3f} < baseline {then:.3f} - {tolerance}")
    search_threshold, cv_threshold = evaluation.production_thresholds()
    for path, threshold in (("dm_with_cv", cv_threshold), ("dm_search_only", search_threshold)):
        r_now = _sweep_row(result[path]["sweep"], threshold)
        r_then = _sweep_row(baseline[path]["sweep"], threshold)
        if r_now["recall"] < r_then["recall"] - tolerance:
            problems.append(f"{path} recall {r_now['recall']:.3f} < {r_then['recall']:.3f} - {tolerance}")
        if r_now["off_target_pass_rate"] > r_then["off_target_pass_rate"] + tolerance:
            problems.append(f"{path} off-target {r_now['off_target_pass_rate']:.3f} > "
                            f"{r_then['off_target_pass_rate']:.3f} + {tolerance}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--models", nargs="+", default=[DEFAULT_MODEL])
    parser.add_argument("--postings", type=Path, default=ROOT / "eval" / "postings.jsonl")
    parser.add_argument("--personas", type=Path, default=ROOT / "eval" / "personas.jsonl")
    parser.add_argument("--no-query-location", action="store_true",
                        help="leave 'Location: ...' out of the Discord query text")
    parser.add_argument("--no-feedback", action="store_true", help="skip the Rocchio simulation")
    parser.add_argument("--job-text", choices=("production", "title"), default="production",
                        help="how postings are turned into text (gosha/evaluation.py job_text)")
    parser.add_argument("--cache-dir", default=str(Path.home() / ".cache" / "gosha-eval"),
                        help="on-disk embedding cache ('' to disable)")
    parser.add_argument("--write-results", type=Path, metavar="DIR",
                        help="write <model>.json per model")
    parser.add_argument("--check", type=Path, metavar="BASELINE_JSON",
                        help="exit 1 if the first model regressed against this baseline")
    parser.add_argument("--tolerance", type=float, default=0.02)
    args = parser.parse_args()

    postings = evaluation.load_postings(args.postings)
    personas = evaluation.load_personas(args.personas)
    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    variant = args.job_text
    results: dict[str, dict] = {}
    for spec in args.models:
        started = time.monotonic()
        encode, info = make_encoder(spec, postings, personas, cache_dir)
        result = evaluation.run(
            postings, personas, encode,
            query_location=None if args.no_query_location else "Cluj",
            with_feedback=not args.no_feedback,
            job_text_variant=variant,
        )
        name = spec if variant == "production" else f"{spec} [{variant}]"
        result = {"model": spec, "job_text": variant, **info, **result}
        results[name] = result
        print(f"{name}: nDCG@10 {result['feed']['mean']['ndcg@10']:.3f} "
              f"({time.monotonic() - started:.0f}s)", file=sys.stderr)
        if args.write_results:  # as each model finishes: a long run can die
            args.write_results.mkdir(parents=True, exist_ok=True)
            suffix = "" if variant == "production" else f".{variant}"
            (args.write_results / f"{slug(spec)}{suffix}.json").write_text(
                json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    summary = summary_markdown(results)
    print(summary)

    if args.check:
        problems = check(results[args.models[0]], args.check, args.tolerance)
        for problem in problems:
            print(f"REGRESSION: {problem}", file=sys.stderr)
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
