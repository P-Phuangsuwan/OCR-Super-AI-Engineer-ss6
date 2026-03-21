from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".pdf"}
PAGE_SUFFIX_PATTERN = re.compile(r"^(?P<base>.+?)(?:_page(?P<page>\d+))?$", re.IGNORECASE)
TABLE_SEPARATOR_PATTERN = re.compile(r"^[\s\-\|\:]+$")


@dataclass(frozen=True)
class ParsedRow:
    id_doc: str
    row_num: str
    party_name: str
    vote: str
    source_file: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Typhoon OCR 1.5 on election images and export rows to CSV."
    )
    parser.add_argument(
        "--input-dir",
        required=True,
        type=Path,
        help="Folder that contains PNG/JPG/PDF files.",
    )
    parser.add_argument(
        "--output-csv",
        default=Path("output/ocr_results.csv"),
        type=Path,
        help="Destination CSV file. Default: output/ocr_results.csv",
    )
    parser.add_argument(
        "--cache-dir",
        default=Path("ocr_cache"),
        type=Path,
        help="Folder used to store OCR markdown for resume/re-run support.",
    )
    parser.add_argument(
        "--sleep-seconds",
        default=3.2,
        type=float,
        help="Delay between OCR API calls. Default 3.2s to stay within 20 req/min.",
    )
    parser.add_argument(
        "--retries",
        default=3,
        type=int,
        help="Retry count when OCR request fails. Default: 3",
    )
    parser.add_argument(
        "--overwrite-cache",
        action="store_true",
        help="Force a fresh OCR call even if a cached markdown file already exists.",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        help="Optional limit for the number of input files, useful while testing.",
    )
    return parser


def normalize_text(text: str) -> str:
    text = text.replace("\u200b", " ")
    text = text.replace("<br>", " ")
    return re.sub(r"\s+", " ", text).strip()


def thai_to_arabic(text: str) -> str:
    return text.translate(THAI_DIGITS)


def digits_only(text: str) -> str:
    normalized = thai_to_arabic(text)
    return "".join(re.findall(r"\d+", normalized))


def natural_sort_key(text: str) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.findall(r"\d+|\D+", text)]


def derive_id_doc(file_path: Path) -> str:
    match = PAGE_SUFFIX_PATTERN.match(file_path.stem)
    if not match:
        return file_path.stem
    return match.group("base")


def derive_page_num(file_path: Path) -> int:
    match = PAGE_SUFFIX_PATTERN.match(file_path.stem)
    if not match or not match.group("page"):
        return 1
    return int(match.group("page"))


def iter_input_files(input_dir: Path) -> list[Path]:
    if not input_dir.exists():
        raise FileNotFoundError(f"Input folder does not exist: {input_dir}")
    if not input_dir.is_dir():
        raise NotADirectoryError(f"Input path is not a folder: {input_dir}")

    files = [path for path in input_dir.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES]
    return sorted(
        files,
        key=lambda path: (
            natural_sort_key(derive_id_doc(path)),
            derive_page_num(path),
            path.name.lower(),
        ),
    )


def get_ocr_markdown(pdf_or_image_path: Path, cache_path: Path, overwrite_cache: bool, retries: int) -> str:
    if cache_path.exists() and not overwrite_cache:
        return cache_path.read_text(encoding="utf-8")

    try:
        from typhoon_ocr import ocr_document
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency: typhoon-ocr\n"
            "Install it with: py -m pip install -r requirements.txt"
        ) from exc

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            markdown = str(ocr_document(pdf_or_image_path=str(pdf_or_image_path)))
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(markdown, encoding="utf-8")
            return markdown
        except Exception as exc:  # pragma: no cover - depends on remote API
            last_error = exc
            wait_seconds = min(12.0, 2.0 * attempt)
            print(
                f"[WARN] OCR failed for {pdf_or_image_path.name} "
                f"(attempt {attempt}/{retries}): {exc}",
                file=sys.stderr,
            )
            if attempt < retries:
                time.sleep(wait_seconds)

    raise RuntimeError(f"OCR failed for {pdf_or_image_path.name}: {last_error}") from last_error


def is_table_separator(line: str) -> bool:
    return bool(TABLE_SEPARATOR_PATTERN.fullmatch(line.strip()))


def looks_like_header(cells: list[str]) -> bool:
    joined = " ".join(cell.lower() for cell in cells)
    header_keywords = (
        "หมายเลข",
        "ชื่อชื่อสกุล",
        "ชื่อ - ชื่อสกุล",
        "ผู้สมัคร",
        "ผู้สมัครรับเลือกตั้ง",
        "สังกัด",
        "พรรคการเมือง",
        "ได้คะแนน",
    )
    return any(keyword in joined for keyword in header_keywords)


def parse_markdown_table(markdown: str, id_doc: str, source_file: str) -> list[ParsedRow]:
    rows: list[ParsedRow] = []
    seen: set[tuple[str, str, str]] = set()

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if "|" not in line:
            continue
        if is_table_separator(line):
            continue

        cells = [normalize_text(cell) for cell in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        if looks_like_header(cells):
            continue

        row_num = digits_only(cells[0])
        party_name = normalize_text(cells[-2])
        vote = digits_only(cells[-1])
        if not row_num or not party_name or not vote:
            continue

        record_key = (row_num, party_name, vote)
        if record_key in seen:
            continue
        seen.add(record_key)

        rows.append(
            ParsedRow(
                id_doc=id_doc,
                row_num=row_num,
                party_name=party_name,
                vote=vote,
                source_file=source_file,
            )
        )

    return rows


def parse_plain_text(markdown: str, id_doc: str, source_file: str) -> list[ParsedRow]:
    rows: list[ParsedRow] = []
    seen: set[tuple[str, str, str]] = set()

    for raw_line in markdown.splitlines():
        line = normalize_text(raw_line)
        if not line:
            continue
        if "|" in line:
            continue

        parts = [normalize_text(part) for part in re.split(r"\s{2,}", line) if normalize_text(part)]
        if len(parts) < 3:
            continue

        row_num = digits_only(parts[0])
        party_name = normalize_text(parts[-2])
        vote = digits_only(parts[-1])
        if not row_num or not party_name or not vote:
            continue

        record_key = (row_num, party_name, vote)
        if record_key in seen:
            continue
        seen.add(record_key)

        rows.append(
            ParsedRow(
                id_doc=id_doc,
                row_num=row_num,
                party_name=party_name,
                vote=vote,
                source_file=source_file,
            )
        )

    return rows


def extract_rows(markdown: str, id_doc: str, source_file: str) -> list[ParsedRow]:
    rows = parse_markdown_table(markdown, id_doc=id_doc, source_file=source_file)
    if rows:
        return rows
    return parse_plain_text(markdown, id_doc=id_doc, source_file=source_file)


def write_csv(rows: Iterable[ParsedRow], output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["id", "id_doc", "row_num", "party_name", "vote"],
        )
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            writer.writerow(
                {
                    "id": index,
                    "id_doc": row.id_doc,
                    "row_num": row.row_num,
                    "party_name": row.party_name,
                    "vote": row.vote,
                }
            )


def main() -> int:
    args = build_parser().parse_args()
    input_files = iter_input_files(args.input_dir)
    if args.max_files is not None:
        input_files = input_files[: args.max_files]

    print(f"[INFO] Found {len(input_files)} files in {args.input_dir}")
    all_rows: list[ParsedRow] = []

    for index, input_file in enumerate(input_files, start=1):
        id_doc = derive_id_doc(input_file)
        cache_path = args.cache_dir / f"{input_file.stem}.md"
        cache_hit = cache_path.exists() and not args.overwrite_cache
        print(f"[INFO] ({index}/{len(input_files)}) OCR: {input_file.name}")

        markdown = get_ocr_markdown(
            pdf_or_image_path=input_file,
            cache_path=cache_path,
            overwrite_cache=args.overwrite_cache,
            retries=args.retries,
        )
        extracted_rows = extract_rows(
            markdown=markdown,
            id_doc=id_doc,
            source_file=input_file.name,
        )
        print(f"[INFO]     Extracted {len(extracted_rows)} rows from {input_file.name}")
        all_rows.extend(extracted_rows)

        if not cache_hit and index < len(input_files):
            time.sleep(args.sleep_seconds)

    write_csv(all_rows, args.output_csv)
    print(f"[INFO] Wrote {len(all_rows)} rows to {args.output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
