from __future__ import annotations

import csv
from pathlib import Path


class CsvShardWriter:
    def __init__(
        self,
        output_dir: str,
        basename: str,
        fieldnames: list[str],
        rows_per_part: int,
        flush_every: int = 1,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.basename = basename
        self.fieldnames = fieldnames
        self.rows_per_part = max(rows_per_part, 1)
        self.flush_every = max(flush_every, 1)
        self.part_index = 0
        self.rows_in_part = 0
        self.total_rows = 0
        self._file = None
        self._writer = None

    def _open_next_part(self) -> None:
        if self._file:
            self._file.close()
        self.part_index += 1
        self.rows_in_part = 0
        path = self.output_dir / f"{self.basename}.part{self.part_index:03d}.csv"
        self._file = path.open("w", encoding="utf-8-sig", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=self.fieldnames, extrasaction="ignore")
        self._writer.writeheader()
        self._file.flush()

    def write_row(self, row: dict[str, object]) -> None:
        if self._writer is None or self.rows_in_part >= self.rows_per_part:
            self._open_next_part()
        assert self._writer is not None
        self._writer.writerow(row)
        self.rows_in_part += 1
        self.total_rows += 1
        if self.total_rows % self.flush_every == 0 and self._file:
            self._file.flush()

    def close(self) -> None:
        if self._file:
            self._file.flush()
            self._file.close()
            self._file = None
            self._writer = None
