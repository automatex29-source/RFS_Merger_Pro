#!/usr/bin/env python3
"""RFS Merger Pro - local backend. Serves the UI at http://localhost:8000 and does the heavy merging."""
import csv, gzip, io, os, re, sys, shutil, socket, tempfile, threading, time, traceback, logging, webbrowser
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List

# Make sure Windows consoles (which often default to cp1252) don't crash on
# the checkmark / accented characters this app prints to the log.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Keep memory low on small cloud instances (Render free = 512 MB): Arrow's
# default allocator holds on to freed memory, and every Polars thread keeps buffers.
os.environ.setdefault("ARROW_DEFAULT_MEMORY_POOL", "system")
os.environ.setdefault("POLARS_MAX_THREADS", "2")
import polars as pl
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("rfs")
BASE = Path(__file__).resolve().parent

app = FastAPI(title="RFS Merger Pro")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    # Catch anything unexpected so one bad request can't take the whole
    # server down, and so the real cause always ends up in this window.
    log.error("Unhandled error on %s %s:\n%s", request.method, request.url.path,
               "".join(traceback.format_exception(exc)))
    return JSONResponse({"success": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500)


class State:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="rfs_"))
        self.parts: List[Path] = []     # one Parquet file per successfully read upload
        self.notes: List[str] = []
        self.columns: List[str] = []
        self.rows = 0

    def reset(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        self.__init__()


S = State()


class ExportConfig(BaseModel):
    columns: List[str] = []
    filters: List[Dict[str, str]] = []
    format: str = "xlsx"


def sniff_csv(path: Path):
    """Return (separator, encoding) from the first 64 KB of a CSV file."""
    with open(path, "rb") as f:
        raw = f.read(65536)
    if len(raw) == 65536 and b"\n" in raw:
        raw = raw[:raw.rindex(b"\n")]
    enc = "utf-8"
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        sep = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|").delimiter
    except csv.Error:
        sep = ","
    return sep, enc


def to_parquet(path: Path) -> Path:
    """Convert one upload to an all-text Parquet file, streaming (low memory)."""
    out = path.with_suffix(".parquet")
    if path.suffix.lower() in (".csv", ".txt"):
        sep, enc = sniff_csv(path)
        if enc not in ("utf-8", "utf-8-sig"):
            # Polars only reads UTF-8: re-encode legacy (Windows) files first
            utf = path.with_suffix(".utf8.csv")
            with open(path, encoding=enc, newline="") as src, open(utf, "w", encoding="utf-8", newline="") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            path.unlink()
            path = utf
        lf = pl.scan_csv(path, separator=sep, infer_schema=False, truncate_ragged_lines=True, raise_if_empty=False)
    else:
        import pandas as pd  # only needed for Excel uploads; keeps idle memory low
        # calamine (Rust) is several times faster and far lighter than openpyxl
        df = pd.read_excel(path, dtype=str, keep_default_na=False, engine="calamine")
        lf = pl.from_pandas(df.astype("string[pyarrow]")).lazy()
        del df
    # Trim header whitespace and keep only the first of any duplicate column
    names, seen, keep = lf.collect_schema().names(), set(), []
    for n in names:
        clean = re.sub(r"_duplicated_\d+$", "", n).lstrip("\ufeff").strip()
        if clean not in seen:
            seen.add(clean)
            keep.append(pl.col(n).cast(pl.String).alias(clean))
    lf.select(keep).sink_parquet(out, row_group_size=50000)
    path.unlink(missing_ok=True)
    return out


def filter_exprs(filters, columns) -> List[pl.Expr]:
    exprs = []
    for f in filters:
        col, val = f.get("column", ""), str(f.get("value", "")).strip().lower()
        op = re.sub(r"[\s_-]", "", f.get("operator", "contains").lower())
        if col not in columns or not val:
            continue
        s = pl.col(col).str.to_lowercase()
        if op in ("equals", "equal", "="):
            cond = s == val
        elif op in ("startswith", "starts"):
            cond = s.str.starts_with(val)
        elif op in ("endswith", "ends"):
            cond = s.str.ends_with(val)
        else:
            cond = s.str.contains(val, literal=True)
        exprs.append(cond)
    return exprs


def merged_batches(cols: List[str], filters) -> Iterator[pl.DataFrame]:
    """Yield the merged, filtered rows in small batches, one file at a time.
    Memory stays flat however many / however big the files are."""
    import pyarrow.parquet as pq
    exprs = filter_exprs(filters, S.columns)
    for part in S.parts:
        for batch in pq.ParquetFile(part).iter_batches(batch_size=50000):
            df = pl.from_arrow(batch)
            missing = [c for c in S.columns if c not in df.columns]
            df = df.with_columns([pl.lit("").alias(c) for c in missing]).fill_null("")
            if exprs:
                df = df.filter(exprs)
            if df.height:
                yield df.select(cols)


_INDEX = (BASE / "static" / "index.html").read_bytes()
_INDEX_GZ = gzip.compress(_INDEX, 6)  # the page is ~900 KB; gzipped it loads ~4x faster


@app.get("/")
async def index(request: Request):
    if "gzip" in request.headers.get("accept-encoding", ""):
        return Response(_INDEX_GZ, media_type="text/html", headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
    return Response(_INDEX, media_type="text/html")


@app.get("/health")
async def health():
    return {"status": "healthy", "files": len(S.parts), "merged": bool(S.columns)}


# Endpoints below are plain `def` so FastAPI runs them in a worker thread:
# a long merge/export then can't freeze the server (and fail Render's health check).
@app.post("/upload")
def upload(files: List[UploadFile] = File(...), append: bool = False):
    """Store and convert uploads. The UI sends one file per request with append=1;
    `.gz` uploads (compressed in the browser) are unpacked here."""
    try:
        if not append:
            S.reset()
        for f in files:
            i = len(S.notes)
            name = re.sub(r"[^\w.\- ]", "_", Path(f.filename or f"file{i}").name)
            gz = name.lower().endswith(".gz")
            if gz:
                name = name[:-3]
            p = S.dir / f"{i}_{name}"
            with open(p, "wb") as out:
                shutil.copyfileobj(gzip.GzipFile(fileobj=f.file) if gz else f.file, out, 1024 * 1024)
            try:
                part = to_parquet(p)
                rows = pl.scan_parquet(part).select(pl.len()).collect().item()
                S.parts.append(part)
                S.notes.append(f"{name}: {rows} rows")
            except Exception as e:
                log.exception("read %s", name)
                p.unlink(missing_ok=True)
                S.notes.append(f"{name}: FAILED ({e})")
        return {"success": True, "file_count": len(S.parts), "notes": S.notes[-len(files):]}
    except Exception as e:
        log.exception("upload")
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


@app.post("/merge")
def merge():
    try:
        if not S.parts:
            raise ValueError("No readable data found. " + "; ".join(S.notes) if S.notes else "No files uploaded")
        S.columns = list(dict.fromkeys(c for p in S.parts for c in pl.scan_parquet(p).collect_schema().names()))
        S.rows = sum(pl.scan_parquet(p).select(pl.len()).collect().item() for p in S.parts)
        log.info("merged %d rows from %d files", S.rows, len(S.parts))
        return {"success": True, "row_count": S.rows, "columns": S.columns, "notes": S.notes}
    except Exception as e:
        log.exception("merge")
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


def write_xlsx(src: Path, out: Path, chunk: int = 100000) -> None:
    """Stream rows from Parquet straight to disk with xlsxwriter (constant memory)."""
    import pyarrow.parquet as pq
    import xlsxwriter
    pf = pq.ParquetFile(src)
    total, cols = pf.metadata.num_rows, pf.schema_arrow.names
    n_sheets = max(1, -(-total // chunk))
    wb = xlsxwriter.Workbook(str(out), {"constant_memory": True})
    ws = wb.add_worksheet("Summary")
    for r, row in enumerate([("Item", "Value"), ("Total Rows", total), ("Total Columns", len(cols)),
                             ("Sheets", n_sheets), ("Export Date", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))]):
        ws.write_row(r, 0, row)
    sheet, r = 0, chunk + 1
    for batch in pf.iter_batches(batch_size=20000):
        for row in zip(*(c.to_pylist() for c in batch.columns)):
            if r > chunk:
                sheet += 1
                ws = wb.add_worksheet(f"Data_{sheet}" if n_sheets > 1 else "Data")
                ws.write_row(0, 0, cols)
                write, r = ws.write_string, 1
            for c, v in enumerate(row):
                if v:
                    write(r, c, v)
            r += 1
    if sheet == 0:
        wb.add_worksheet("Data").write_row(0, 0, cols)
    wb.close()


@app.post("/export")
def export(cfg: ExportConfig):
    try:
        if not S.columns:
            raise ValueError("Merge files first")
        import pyarrow as pa
        import pyarrow.parquet as pq
        cols = [c for c in (cfg.columns or S.columns) if c in S.columns]
        stamp = f"RFS_Merged_{datetime.now():%Y%m%d_%H%M%S}"
        if cfg.format == "csv":
            out = S.dir / "export.csv"
            with open(out, "wb") as f:
                f.write("\ufeff".encode())  # BOM so Excel opens UTF-8 correctly
                pl.DataFrame(schema={c: pl.String for c in cols}).write_csv(f)
                for df in merged_batches(cols, cfg.filters):
                    df.write_csv(f, include_header=False)
            return FileResponse(out, filename=stamp + ".csv", media_type="text/csv")
        tmp, out = S.dir / "export.parquet", S.dir / "export.xlsx"
        schema = pa.schema([(c, pa.string()) for c in cols])
        with pq.ParquetWriter(tmp, schema) as w:
            for df in merged_batches(cols, cfg.filters):
                w.write_table(df.to_arrow().cast(schema))
        write_xlsx(tmp, out)
        tmp.unlink(missing_ok=True)
        return FileResponse(out, filename=stamp + ".xlsx",
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:
        log.exception("export")
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


@app.post("/reset")
def reset():
    S.reset()
    return {"status": "reset"}


def free_port(start=8000, tries=20):
    for p in range(start, start + tries):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


if __name__ == "__main__":
    import uvicorn
    port = free_port()
    url = f"http://localhost:{port}"
    print("\n  RFS Merger Pro is running at " + url + "\n  Close this window to stop it.\n")
    threading.Thread(target=lambda: (time.sleep(1.5), webbrowser.open(url)), daemon=True).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutil.rmtree(S.dir, ignore_errors=True)
