from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".pdf"}
PAGE_SUFFIX_PATTERN = re.compile(r"^(?P<base>.+?)(?:_page(?P<page>\d+))?$", re.IGNORECASE)
TABLE_SEPARATOR_PATTERN = re.compile(r"^[\s\-\|\:]+$")
HTML_ROW_PATTERN = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
HTML_CELL_PATTERN = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class ParsedRow:
    id_doc: str
    row_num: str
    party_name: str
    vote: str
    source_file: str


class RateLimiter:
    def __init__(self, max_requests_per_minute: int) -> None:
        self.max_requests_per_minute = max_requests_per_minute
        self._lock = threading.Lock()
        self._request_times: deque[float] = deque()

    def wait_for_turn(self) -> None:
        if self.max_requests_per_minute <= 0:
            return

        while True:
            with self._lock:
                now = time.monotonic()
                while self._request_times and now - self._request_times[0] >= 60.0:
                    self._request_times.popleft()

                if len(self._request_times) < self.max_requests_per_minute:
                    self._request_times.append(now)
                    return

                wait_seconds = 60.0 - (now - self._request_times[0])

            time.sleep(max(wait_seconds, 0.05))


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
    parser.add_argument(
        "--page-mode",
        choices=["all", "likely-tables"],
        default="all",
        help="Choose whether to OCR all pages or only pages most likely to contain result tables.",
    )
    parser.add_argument(
        "--workers",
        default=1,
        type=int,
        help="Number of concurrent OCR workers. Default: 1",
    )
    parser.add_argument(
        "--max-requests-per-minute",
        default=20,
        type=int,
        help="Global rate limit for OCR request starts across all workers. Default: 20",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Log OCR failures and continue with the remaining files instead of aborting the whole batch.",
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


def is_party_list_doc(id_doc: str) -> bool:
    return id_doc.lower().startswith("party_list_")


def select_likely_table_pages(files: list[Path]) -> list[Path]:
    grouped: dict[str, list[Path]] = {}
    for file_path in files:
        grouped.setdefault(derive_id_doc(file_path), []).append(file_path)

    selected: list[Path] = []
    for id_doc, group_files in grouped.items():
        sorted_group_files = sorted(group_files, key=lambda path: natural_sort_key(path.name))
        if is_party_list_doc(id_doc):
            # Most party-list documents keep the result table on pages 2-5, but
            # shorter files sometimes start directly on page 1 and continue on
            # page 2/3 without a separate cover page.
            table_pages = [path for path in sorted_group_files if 2 <= derive_page_num(path) <= 5]
            page_one = [path for path in sorted_group_files if derive_page_num(path) == 1]
            if page_one and len(sorted_group_files) <= 3:
                table_pages = page_one + table_pages
            if table_pages:
                selected.extend(
                    sorted(
                        table_pages,
                        key=lambda path: (
                            derive_page_num(path),
                            path.name.lower(),
                        ),
                    )
                )
                continue

        page_two = [path for path in sorted_group_files if derive_page_num(path) == 2]
        if page_two:
            selected.append(sorted(page_two, key=lambda path: natural_sort_key(path.name))[0])
            continue

        page_one = [path for path in sorted_group_files if derive_page_num(path) == 1]
        if page_one:
            selected.append(sorted(page_one, key=lambda path: natural_sort_key(path.name))[0])
            continue

        selected.extend(sorted_group_files[:1])

    return sorted(
        selected,
        key=lambda path: (
            natural_sort_key(derive_id_doc(path)),
            derive_page_num(path),
            path.name.lower(),
        ),
    )


def iter_input_files(input_dir: Path, page_mode: str = "all") -> list[Path]:
    if not input_dir.exists():
        raise FileNotFoundError(f"Input folder does not exist: {input_dir}")
    if not input_dir.is_dir():
        raise NotADirectoryError(f"Input path is not a folder: {input_dir}")

    files = [path for path in input_dir.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES]
    files = sorted(
        files,
        key=lambda path: (
            natural_sort_key(derive_id_doc(path)),
            derive_page_num(path),
            path.name.lower(),
        ),
    )
    if page_mode == "likely-tables":
        return select_likely_table_pages(files)
    return files


def get_ocr_markdown(
    pdf_or_image_path: Path,
    cache_path: Path,
    overwrite_cache: bool,
    retries: int,
    before_request: callable | None = None,
) -> str:
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
            if before_request is not None:
                before_request()
            markdown = str(ocr_document(pdf_or_image_path=str(pdf_or_image_path)))
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(markdown, encoding="utf-8")
            return markdown
        except Exception as exc:  # pragma: no cover - depends on remote API
            last_error = exc
            error_text = str(exc).lower()
            if "rate exceeded" in error_text or "ratelimit" in error_text:
                wait_seconds = min(180.0, 30.0 * attempt)
            elif "timed out" in error_text or "error code: 408" in error_text:
                wait_seconds = min(150.0, 25.0 * attempt)
            elif "server error" in error_text or "can't process the request" in error_text or "error code: 500" in error_text:
                wait_seconds = min(150.0, 25.0 * attempt)
            else:
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


def strip_html_tags(text: str) -> str:
    return normalize_text(html.unescape(HTML_TAG_PATTERN.sub(" ", text)))


def looks_like_non_result_row(text: str) -> bool:
    normalized = normalize_text(text).lower()
    noise_keywords = (
        "รวมคะแนนทั้งสิ้น",
        "คะแนนทั้งหมด",
        "ประกาศ ณ วันที่",
        "ลงชื่อ",
        "ประธานกรรมการ",
        "กรรมการการเลือกตั้ง",
    )
    return any(keyword in normalized for keyword in noise_keywords)


def build_row_from_cells(
    cells: list[str],
    id_doc: str,
    source_file: str,
    seen: set[tuple[str, str, str]],
) -> ParsedRow | None:
    normalized_cells = [normalize_text(cell) for cell in cells]
    if len(normalized_cells) < 3:
        return None
    if looks_like_header(normalized_cells):
        return None

    row_num = digits_only(normalized_cells[0])
    vote_idx: int | None = None
    party_idx: int | None = None

    if is_party_list_doc(id_doc):
        if row_num:
            if len(normalized_cells) >= 3:
                party_idx = 1
                vote_idx = 2
        else:
            if len(normalized_cells) >= 2:
                party_idx = 0
                vote_idx = 1
    else:
        if not row_num or len(normalized_cells) < 4:
            return None

        if (
            len(normalized_cells) >= 5
            and digits_only(normalized_cells[-1])
            and digits_only(normalized_cells[-2])
            and len(digits_only(normalized_cells[-1])) <= 2
        ):
            party_idx = 2
            vote_idx = len(normalized_cells) - 2
        else:
            for idx in range(len(normalized_cells) - 1, 0, -1):
                if digits_only(normalized_cells[idx]):
                    vote_idx = idx
                    break
            if vote_idx is None or vote_idx < 1:
                return None
            party_idx = 2 if len(normalized_cells) >= 4 else vote_idx - 1

    if vote_idx is None or party_idx is None:
        return None

    party_name = normalize_text(normalized_cells[party_idx])
    vote = digits_only(normalized_cells[vote_idx])
    if not party_name or not vote or looks_like_non_result_row(party_name):
        return None

    record_key = (row_num, party_name, vote)
    if record_key in seen:
        return None
    seen.add(record_key)

    return ParsedRow(
        id_doc=id_doc,
        row_num=row_num,
        party_name=party_name,
        vote=vote,
        source_file=source_file,
    )


def fill_missing_row_numbers(rows: list[ParsedRow]) -> list[ParsedRow]:
    grouped: dict[str, list[ParsedRow]] = {}
    for row in rows:
        grouped.setdefault(row.id_doc, []).append(row)

    completed_rows: list[ParsedRow] = []
    for id_doc, doc_rows in grouped.items():
        if not is_party_list_doc(id_doc):
            completed_rows.extend(doc_rows)
            continue

        pending_indexes: list[int] = []
        previous_number: int | None = None
        doc_rows_copy = list(doc_rows)

        for index, row in enumerate(doc_rows_copy):
            if row.row_num:
                current_number = int(row.row_num)
                if pending_indexes:
                    if previous_number is not None:
                        gap = current_number - previous_number - 1
                        if gap > 0:
                            assignable = min(len(pending_indexes), gap)
                            for offset, pending_index in enumerate(pending_indexes[:assignable], start=1):
                                pending_row = doc_rows_copy[pending_index]
                                doc_rows_copy[pending_index] = ParsedRow(
                                    id_doc=pending_row.id_doc,
                                    row_num=str(previous_number + offset),
                                    party_name=pending_row.party_name,
                                    vote=pending_row.vote,
                                    source_file=pending_row.source_file,
                                )
                    pending_indexes = []
                previous_number = current_number
                continue

            pending_indexes.append(index)

        if pending_indexes and previous_number is not None:
            for offset, pending_index in enumerate(pending_indexes, start=1):
                pending_row = doc_rows_copy[pending_index]
                doc_rows_copy[pending_index] = ParsedRow(
                    id_doc=pending_row.id_doc,
                    row_num=str(previous_number + offset),
                    party_name=pending_row.party_name,
                    vote=pending_row.vote,
                    source_file=pending_row.source_file,
                )

        completed_rows.extend(doc_rows_copy)

    return completed_rows


def parse_html_table(markdown: str, id_doc: str, source_file: str) -> list[ParsedRow]:
    rows: list[ParsedRow] = []
    seen: set[tuple[str, str, str]] = set()

    for row_html in HTML_ROW_PATTERN.findall(markdown):
        cells = [strip_html_tags(cell) for cell in HTML_CELL_PATTERN.findall(row_html)]
        parsed = build_row_from_cells(
            cells=cells,
            id_doc=id_doc,
            source_file=source_file,
            seen=seen,
        )
        if parsed is not None:
            rows.append(parsed)

    return rows


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
        parsed = build_row_from_cells(
            cells=cells,
            id_doc=id_doc,
            source_file=source_file,
            seen=seen,
        )
        if parsed is not None:
            rows.append(parsed)

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
        parsed = build_row_from_cells(
            cells=parts,
            id_doc=id_doc,
            source_file=source_file,
            seen=seen,
        )
        if parsed is not None:
            rows.append(parsed)

    return rows


def extract_rows(markdown: str, id_doc: str, source_file: str) -> list[ParsedRow]:
    rows = parse_html_table(markdown, id_doc=id_doc, source_file=source_file)
    if rows:
        return rows

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


def process_one_file(
    file_index: int,
    total_files: int,
    input_file: Path,
    cache_dir: Path,
    overwrite_cache: bool,
    retries: int,
    rate_limiter: RateLimiter,
    continue_on_error: bool,
) -> tuple[int, list[ParsedRow]]:
    id_doc = derive_id_doc(input_file)
    cache_path = cache_dir / f"{input_file.stem}.md"
    cache_hit = cache_path.exists() and not overwrite_cache
    print(f"[INFO] ({file_index}/{total_files}) OCR: {input_file.name}")

    try:
        markdown = get_ocr_markdown(
            pdf_or_image_path=input_file,
            cache_path=cache_path,
            overwrite_cache=overwrite_cache,
            retries=retries,
            before_request=None if cache_hit else rate_limiter.wait_for_turn,
        )
    except Exception as exc:
        if not continue_on_error:
            raise
        print(f"[ERROR] Skipping {input_file.name}: {exc}", file=sys.stderr)
        return file_index, []

    extracted_rows = extract_rows(
        markdown=markdown,
        id_doc=id_doc,
        source_file=input_file.name,
    )
    print(f"[INFO]     Extracted {len(extracted_rows)} rows from {input_file.name}")
    return file_index, extracted_rows


def main() -> int:
    args = build_parser().parse_args()
    input_files = iter_input_files(args.input_dir, page_mode=args.page_mode)
    if args.max_files is not None:
        input_files = input_files[: args.max_files]

    print(
        f"[INFO] Found {len(input_files)} files in {args.input_dir} "
        f"(page_mode={args.page_mode}, workers={args.workers})"
    )
    rate_limiter = RateLimiter(args.max_requests_per_minute)

    if args.workers <= 1:
        all_rows: list[ParsedRow] = []
        for index, input_file in enumerate(input_files, start=1):
            _, extracted_rows = process_one_file(
                file_index=index,
                total_files=len(input_files),
                input_file=input_file,
                cache_dir=args.cache_dir,
                overwrite_cache=args.overwrite_cache,
                retries=args.retries,
                rate_limiter=rate_limiter,
                continue_on_error=args.continue_on_error,
            )
            all_rows.extend(extracted_rows)
            cache_path = args.cache_dir / f"{input_file.stem}.md"
            cache_hit = cache_path.exists() and not args.overwrite_cache
            if not cache_hit and args.sleep_seconds > 0 and index < len(input_files):
                time.sleep(args.sleep_seconds)
    else:
        rows_by_file_index: dict[int, list[ParsedRow]] = {}
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_map = {
                executor.submit(
                    process_one_file,
                    index,
                    len(input_files),
                    input_file,
                    args.cache_dir,
                    args.overwrite_cache,
                    args.retries,
                    rate_limiter,
                    args.continue_on_error,
                ): index
                for index, input_file in enumerate(input_files, start=1)
            }
            for future in as_completed(future_map):
                file_index, extracted_rows = future.result()
                rows_by_file_index[file_index] = extracted_rows

        all_rows = []
        for file_index in sorted(rows_by_file_index):
            all_rows.extend(rows_by_file_index[file_index])

    all_rows = fill_missing_row_numbers(all_rows)
    write_csv(all_rows, args.output_csv)
    print(f"[INFO] Wrote {len(all_rows)} rows to {args.output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
