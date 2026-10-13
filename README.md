# Web & Document Extraction Pipeline

**Convert document-detail URLs and mixed attachments into structured text files that a research, reporting or downstream AI workflow can consume.**

This Python project separates discovery from extraction. HTTP requests handle the main workload; selected records can be reviewed through Playwright when rendered content is needed. Outputs are streamed into manageable CSV shards instead of one oversized file.

## Input, processing and delivery

| Stage | Result |
| --- | --- |
| Read `list_documents.csv` | Stream rows without loading the entire input into memory |
| Clean and fetch detail URLs | Normalize whitespace, extract body text and discover attachments |
| Process attachments | Extract PDF, DOCX, RTF, text and supported ZIP contents |
| Handle scans/images | Optional OCR with explicit success/failure status |
| Isolate difficult records | A separate browser-review queue rather than a browser for every request |
| Export | Sharded CSV files, status fields and summary reports |

## Why this is useful to a client

The deliverable is a traceable text dataset, not a pile of downloaded pages. Status fields distinguish missing attachments, failed requests, OCR results and unsupported formats so a client can inspect what needs further work.

## Quick start on Windows

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pccz_list_documents.pipeline --help
```

Start with a small input sample and an output directory dedicated to that run:

```powershell
python -m pccz_list_documents.pipeline --input-csv list_documents.csv --output-dir data/output/sample100 --start-row 1 --limit 100 --detail-workers 4 --max-in-flight 16 --flush-every 1
```

The input expects the Chinese URL field `文书详情链接`; keep the schema or explicitly adapt the code for a new dataset. Install Chromium with `python -m playwright install chromium` only if using the browser stage.

## Modules

`discover.py` identifies content and attachment links. `extract_stage.py` runs text extraction. `attachments.py` handles formats and OCR. `fetch_requests.py` and `fetch_playwright.py` implement access paths. `writer.py` streams CSV shards. `launcher.py` partitions work across independent batches.

## Limits and acceptance criteria

- Legacy `.doc`/`.wps` files are marked as requiring an external converter; they are not silently reported as extracted.
- OCR quality depends on scan quality, language, page limits and installed dependencies.
- CSV sharding and range-based scheduling are implemented; **no claim of a verified ten-million-record run or throughput benchmark is made**.
- Use a new output directory for each run unless you have reviewed overwrite/resume behavior. Streamed output alone is not a guarantee of automatic recovery.
- Target access, rate limits, expected formats and data-use permissions must be agreed before live collection.

For a client handover, define a representative input sample, required fields, attachment types, acceptable failure categories and the output schema first.

[Detailed Chinese operating notes](docs/legacy-readme.zh-CN.md)
