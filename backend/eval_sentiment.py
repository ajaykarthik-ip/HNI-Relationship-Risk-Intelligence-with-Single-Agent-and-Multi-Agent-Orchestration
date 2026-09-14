#!/usr/bin/env python3
"""Measure sentiment classification against the hand-labelled set.

    python eval_sentiment.py
    python eval_sentiment.py --show-errors

Reports per-class precision, recall and F1, plus a confusion matrix. The
labelled set lives in eval/headlines.json and includes cases the domain
lexicon is expected to miss, so the figure is honest rather than flattering.
"""

from __future__ import annotations

import argparse
import json
import pathlib
from collections import defaultdict

from affluense.enrich.sentiment import SentimentAnalyzer

CLASSES = ["negative", "neutral", "positive"]


def evaluate(items: list, analyzer: SentimentAnalyzer) -> tuple:
    matrix = defaultdict(lambda: defaultdict(int))
    errors = []

    for item in items:
        gold = item["label"]
        predicted, score = analyzer.classify(item["headline"])
        matrix[gold][predicted] += 1
        if predicted != gold:
            errors.append((item["headline"], gold, predicted, score))

    return matrix, errors


def metrics(matrix: dict) -> dict:
    result = {}
    for label in CLASSES:
        true_positive = matrix[label][label]
        predicted_total = sum(matrix[g][label] for g in CLASSES)
        actual_total = sum(matrix[label].values())

        precision = true_positive / predicted_total if predicted_total else 0.0
        recall = true_positive / actual_total if actual_total else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        result[label] = {
            "precision": precision, "recall": recall, "f1": f1,
            "support": actual_total,
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show-errors", action="store_true",
                        help="Print every misclassified headline")
    parser.add_argument("--set", default="eval/headlines.json")
    args = parser.parse_args()

    data = json.loads(pathlib.Path(args.set).read_text(encoding="utf-8"))
    items = data["items"]

    analyzer = SentimentAnalyzer()
    matrix, errors = evaluate(items, analyzer)
    scores = metrics(matrix)

    total = len(items)
    correct = total - len(errors)

    print(f"\n  Sentiment evaluation — {total} hand-labelled headlines")
    print(f"  Backend: {analyzer.backend}")
    if "DEGRADED" in analyzer.backend:
        print("  WARNING: this is the fallback, not the real classifier.")
        print("           Run: pip install -r requirements.txt")
    print()
    print(f"  {'class':10} {'precision':>10} {'recall':>8} {'f1':>7} {'support':>8}")
    for label in CLASSES:
        s = scores[label]
        print(f"  {label:10} {s['precision']:>10.2f} {s['recall']:>8.2f} "
              f"{s['f1']:>7.2f} {s['support']:>8}")

    macro_f1 = sum(scores[c]["f1"] for c in CLASSES) / len(CLASSES)
    print(f"\n  accuracy  {correct}/{total} = {correct / total:.2f}")
    print(f"  macro F1  {macro_f1:.2f}")

    print("\n  Confusion matrix (rows: actual, columns: predicted)")
    print(f"  {'':10} " + " ".join(f"{c:>9}" for c in CLASSES))
    for gold in CLASSES:
        row = " ".join(f"{matrix[gold][p]:>9}" for p in CLASSES)
        print(f"  {gold:10} {row}")

    # Missing adverse coverage is the costly error in screening; calling
    # ordinary news negative only wastes an analyst's time.
    missed_negatives = sum(matrix["negative"][p] for p in ("neutral", "positive"))
    print(f"\n  Negatives missed (the costly error): {missed_negatives} "
          f"of {scores['negative']['support']}")

    if args.show_errors and errors:
        print("\n  Misclassified:")
        for headline, gold, predicted, score in errors:
            print(f"    gold={gold:9} got={predicted:9} ({score:+.2f})  {headline[:72]}")
    elif errors:
        print(f"\n  {len(errors)} misclassified. Re-run with --show-errors to list them.")

    print()


if __name__ == "__main__":
    main()
