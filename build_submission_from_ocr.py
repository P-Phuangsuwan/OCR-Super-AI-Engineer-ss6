from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from collections import defaultdict


PUNCTUATION_PATTERN = re.compile(r"[\s\-\.,/()_:;'\"]+")
NUMBER_FRAGMENT_PATTERN = re.compile(r"[0-9๐-๙,]{2,}")
THAI_DIGIT_TRANSLATION = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
GOOD_BALLOT_MARKER = "\u0e1a\u0e31\u0e15\u0e23\u0e14\u0e35"
COMMON_NAME_REPLACEMENTS = {
    "ประชาธิปัตย์ใหม่": "ประชาธิปไตยใหม่",
    "สังคมประชาธิปัตย์ใหม่": "สังคมประชาธิปไตยไทย",
    "สังคมประชาธิปัตย์": "สังคมประชาธิปไตย",
    "วิชชินใหม่": "วิชชั่นใหม่",
    "วิชช์ใหม่": "วิชชั่นใหม่",
    "วิชชันใหม่": "วิชชั่นใหม่",
    "คลังไทย": "คลองไทย",
    "รวมไทยใหม่": "รวมใจไทย",
}


@dataclass(frozen=True)
class OCRRow:
    id_doc: str
    row_num: str
    party_name: str
    vote: str


@dataclass(frozen=True)
class TemplateRow:
    id: str
    doc_id: str
    row_num: str
    party_name: str


@dataclass(frozen=True)
class MatchResult:
    vote: str
    source: str
    score: float
    matched_row_num: str
    matched_party_name: str


def extract_numeric_fragment(value: str) -> int | None:
    normalized = value.translate(THAI_DIGIT_TRANSLATION)
    digits_only = re.sub(r"[^0-9]", "", normalized)
    return int(digits_only) if digits_only else None


def iter_page1_cache_dirs() -> tuple[Path, ...]:
    candidates = [Path("ocr_cache")]
    candidates.extend(sorted(Path(".").glob("temp_constituency_page1*_cache")))

    unique_paths: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        normalized = path.resolve(strict=False)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique_paths.append(path)
    return tuple(unique_paths)


def extract_good_ballots_from_page_cache(id_doc: str) -> int | None:
    for cache_dir in iter_page1_cache_dirs():
        for candidate_name in (f"{id_doc}.md", f"{id_doc}_page1.md"):
            cache_path = cache_dir / candidate_name
            if not cache_path.exists():
                continue

            text = cache_path.read_text(encoding="utf-8")
            marker_index = text.find(GOOD_BALLOT_MARKER)
            if marker_index < 0:
                continue

            snippet = text[max(0, marker_index - 20) : marker_index + 120]
            matches = NUMBER_FRAGMENT_PATTERN.findall(snippet)
            if not matches:
                continue

            return extract_numeric_fragment(matches[0])

    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build submission CSV from OCR results and the current template."
    )
    parser.add_argument(
        "--ocr-csv",
        default=Path("output/ocr_results_refresh.csv"),
        type=Path,
        help="OCR result CSV. Default: output/ocr_results_refresh.csv",
    )
    parser.add_argument(
        "--template-csv",
        default=Path(r"C:\Users\Pc\Downloads\summission_template2.csv"),
        type=Path,
        help="Submission template CSV.",
    )
    parser.add_argument(
        "--output-csv",
        default=Path("output/submission_id_votes_targeted.csv"),
        type=Path,
        help="Output submission CSV with id,votes columns.",
    )
    parser.add_argument(
        "--diagnostics-json",
        default=Path("output/submission_id_votes_targeted_diagnostics.json"),
        type=Path,
        help="Diagnostics report for zeros and fuzzy matches.",
    )
    return parser


def normalize_party_name(value: str) -> str:
    normalized = value.strip().lower()
    normalized = PUNCTUATION_PATTERN.sub("", normalized)
    normalized = re.sub(r"^พรรค", "", normalized)
    for source, target in COMMON_NAME_REPLACEMENTS.items():
        normalized = normalized.replace(source, target)
    return normalized


def party_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        shorter = min(len(left), len(right))
        longer = max(len(left), len(right))
        if longer == 0:
            return 0.0
        return 0.88 + (shorter / longer) * 0.12
    return difflib.SequenceMatcher(a=left, b=right).ratio()


def load_ocr_rows(path: Path) -> list[OCRRow]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            OCRRow(
                id_doc=row["id_doc"],
                row_num=row["row_num"],
                party_name=row["party_name"],
                vote=row["vote"],
            )
            for row in csv.DictReader(handle)
        ]


def load_template_rows(path: Path) -> list[TemplateRow]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            TemplateRow(
                id=row["id"],
                doc_id=row["doc_id"],
                row_num=row["row_num"],
                party_name=row["party_name"],
            )
            for row in csv.DictReader(handle)
        ]


def row_distance_score(template_row_num: str, candidate_row_num: str) -> tuple[int, float]:
    if not template_row_num or not candidate_row_num:
        return 999, 0.0

    distance = abs(int(template_row_num) - int(candidate_row_num))
    if distance == 0:
        return distance, 0.15
    if distance == 1:
        return distance, 0.08
    if distance <= 3:
        return distance, 0.03
    if distance <= 6:
        return distance, 0.01
    return distance, 0.0


def score_candidate(
    template_row: TemplateRow,
    candidate: OCRRow,
    *,
    party_list_doc: bool,
) -> tuple[float, float, int]:
    template_name = normalize_party_name(template_row.party_name)
    candidate_name = normalize_party_name(candidate.party_name)
    similarity = party_similarity(template_name, candidate_name)
    row_distance, row_bonus = row_distance_score(template_row.row_num, candidate.row_num)

    score = similarity
    if party_list_doc:
        score += row_bonus
    elif row_distance == 0:
        score += 0.01

    return score, similarity, row_distance


def choose_best_match(
    template_row: TemplateRow,
    candidates: list[OCRRow],
    *,
    allow_row_exact_only: bool = True,
) -> MatchResult | None:
    if not candidates:
        return None

    party_list_doc = template_row.doc_id.startswith("party_list_")
    exact_name = normalize_party_name(template_row.party_name)

    ranked: list[tuple[float, float, int, OCRRow]] = []
    for candidate in candidates:
        score, similarity, row_distance = score_candidate(
            template_row,
            candidate,
            party_list_doc=party_list_doc,
        )
        ranked.append((score, similarity, row_distance, candidate))

    ranked.sort(
        key=lambda item: (
            item[0],
            item[1],
            -len(item[3].vote),
            -int(item[3].vote or "0"),
            -int(item[3].row_num or "0"),
        ),
        reverse=True,
    )
    best_score, best_similarity, best_row_distance, best = ranked[0]

    if normalize_party_name(best.party_name) == exact_name:
        if party_list_doc and best_row_distance == 0:
            source = "row_exact_party_exact"
        elif party_list_doc and best_row_distance <= 3:
            source = "row_near_party_exact"
        else:
            source = "party_exact"
        return MatchResult(
            vote=best.vote,
            source=source,
            score=best_score,
            matched_row_num=best.row_num,
            matched_party_name=best.party_name,
        )

    if allow_row_exact_only and party_list_doc and best_row_distance == 0 and len(candidates) == 1:
        return MatchResult(
            vote=best.vote,
            source="row_exact_only",
            score=best_score,
            matched_row_num=best.row_num,
            matched_party_name=best.party_name,
        )

    if party_list_doc and best_row_distance <= 1 and best_similarity >= 0.72:
        return MatchResult(
            vote=best.vote,
            source="row_near_party_fuzzy",
            score=best_score,
            matched_row_num=best.row_num,
            matched_party_name=best.party_name,
        )

    if best_similarity >= 0.86:
        return MatchResult(
            vote=best.vote,
            source="party_fuzzy_strong",
            score=best_score,
            matched_row_num=best.row_num,
            matched_party_name=best.party_name,
        )

    if best_similarity >= 0.76 and best_row_distance <= 3:
        return MatchResult(
            vote=best.vote,
            source="party_fuzzy_near",
            score=best_score,
            matched_row_num=best.row_num,
            matched_party_name=best.party_name,
        )

    return None


def build_submission(
    template_rows: list[TemplateRow],
    ocr_rows: list[OCRRow],
) -> tuple[list[dict[str, str]], dict[str, object]]:
    rows_by_doc: dict[str, list[OCRRow]] = {}
    rows_by_doc_and_row: dict[tuple[str, str], list[OCRRow]] = {}
    rows_by_doc_and_party: dict[tuple[str, str], list[OCRRow]] = {}

    for row in ocr_rows:
        rows_by_doc.setdefault(row.id_doc, []).append(row)
        rows_by_doc_and_row.setdefault((row.id_doc, row.row_num), []).append(row)
        normalized_party = normalize_party_name(row.party_name)
        rows_by_doc_and_party.setdefault((row.id_doc, normalized_party), []).append(row)

    submission_rows: list[dict[str, str]] = []
    diagnostics: list[dict[str, object]] = []

    for template_row in template_rows:
        normalized_party = normalize_party_name(template_row.party_name)
        party_list_doc = template_row.doc_id.startswith("party_list_")
        doc_rows = rows_by_doc.get(template_row.doc_id, [])
        exact_row_candidates = rows_by_doc_and_row.get((template_row.doc_id, template_row.row_num), [])

        candidate_groups: list[list[OCRRow]] = []
        if party_list_doc:
            candidate_groups.append(exact_row_candidates)

            if template_row.row_num:
                row_number = int(template_row.row_num)
                nearby_rows: list[OCRRow] = []
                for delta in (1, -1, 2, -2, 3, -3):
                    nearby_rows.extend(rows_by_doc_and_row.get((template_row.doc_id, str(row_number + delta)), []))
                candidate_groups.append(nearby_rows)

        candidate_groups.append(rows_by_doc_and_party.get((template_row.doc_id, normalized_party), []))
        candidate_groups.append(doc_rows)

        selected: MatchResult | None = None
        for candidates in candidate_groups:
            selected = choose_best_match(
                template_row,
                candidates,
                allow_row_exact_only=False,
            )
            if selected is not None:
                break

        if selected is None and party_list_doc:
            selected = choose_best_match(
                template_row,
                exact_row_candidates,
                allow_row_exact_only=True,
            )

        vote = selected.vote if selected is not None else "0"
        submission_rows.append({"id": template_row.id, "votes": vote})

        if (
            selected is None
            or selected.source.startswith("party_fuzzy")
            or selected.source.startswith("row_near")
            or selected.source == "row_exact_only"
        ):
            diagnostics.append(
                {
                    "id": template_row.id,
                    "doc_id": template_row.doc_id,
                    "row_num": template_row.row_num,
                    "party_name": template_row.party_name,
                    "predicted_vote": vote,
                    "match_source": "zero" if selected is None else selected.source,
                    "match_score": None if selected is None else round(selected.score, 4),
                    "matched_row_num": "" if selected is None else selected.matched_row_num,
                    "matched_party_name": "" if selected is None else selected.matched_party_name,
                }
            )

    submission_by_id = {row["id"]: row for row in submission_rows}
    diagnostics_by_id = {row["id"]: row for row in diagnostics}
    ocr_sum_by_doc: dict[str, int] = defaultdict(int)
    unresolved_constituency_rows: dict[str, list[TemplateRow]] = defaultdict(list)

    for row in ocr_rows:
        vote_value = extract_numeric_fragment(row.vote)
        if vote_value is not None:
            ocr_sum_by_doc[row.id_doc] += vote_value

    for template_row in template_rows:
        if not template_row.doc_id.startswith("constituency_"):
            continue
        if not template_row.party_name.strip():
            continue
        if submission_by_id[template_row.id]["votes"] == "0":
            unresolved_constituency_rows[template_row.doc_id].append(template_row)

    for doc_id, missing_rows in unresolved_constituency_rows.items():
        if len(missing_rows) != 1:
            continue

        good_ballots = extract_good_ballots_from_page_cache(doc_id)
        if good_ballots is None:
            continue

        inferred_vote = good_ballots - ocr_sum_by_doc.get(doc_id, 0)
        if inferred_vote <= 0 or inferred_vote >= good_ballots:
            continue

        target_row = missing_rows[0]
        submission_by_id[target_row.id]["votes"] = str(inferred_vote)
        diagnostics_by_id[target_row.id] = {
            "id": target_row.id,
            "doc_id": target_row.doc_id,
            "row_num": target_row.row_num,
            "party_name": target_row.party_name,
            "predicted_vote": str(inferred_vote),
            "match_source": "good_ballot_balance",
            "match_score": 1.0,
            "matched_row_num": "",
            "matched_party_name": "",
        }

    diagnostics = list(diagnostics_by_id.values())

    summary = {
        "zero_votes": sum(1 for row in submission_rows if row["votes"] == "0"),
        "fuzzy_or_zero_rows": len(diagnostics),
    }
    return submission_rows, {"summary": summary, "rows": diagnostics}


def write_submission(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "votes"])
        writer.writeheader()
        writer.writerows(rows)


def write_diagnostics(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    args = build_parser().parse_args()
    ocr_rows = load_ocr_rows(args.ocr_csv)
    template_rows = load_template_rows(args.template_csv)
    submission_rows, diagnostics = build_submission(template_rows, ocr_rows)
    write_submission(args.output_csv, submission_rows)
    write_diagnostics(args.diagnostics_json, diagnostics)
    print(f"[INFO] Wrote {len(submission_rows)} rows to {args.output_csv}")
    print(f"[INFO] Zero votes: {diagnostics['summary']['zero_votes']}")
    print(f"[INFO] Diagnostics rows: {diagnostics['summary']['fuzzy_or_zero_rows']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
