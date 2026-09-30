#!/usr/bin/env python3
"""RFS Merger Pro - local backend. Serves the UI at http://localhost:8000 and does the heavy merging."""
import gc, io, os, re, sys, shutil, socket, tempfile, threading, time, traceback, logging, webbrowser
from datetime import datetime
from pathlib import Path
from typing import Dict, List

# Make sure Windows consoles (which often default to cp1252) don't crash on
# the checkmark / accented characters this app prints to the log.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import pandas as pd
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("rfs")
BASE = Path(__file__).resolve().parent

app = FastAPI(title="RFS Merger Pro")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Arrow-backed strings use a fraction of the memory of plain Python strings,
# which matters a lot on small cloud instances (e.g. Render's 512 MB).
try:
    import pyarrow  # noqa: F401
    STR = "string[pyarrow]"
except ImportError:
    STR = "string"


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
        self.files: List[Path] = []
        self.df = None

    def reset(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        self.__init__()


S = State()


class ExportConfig(BaseModel):
    columns: List[str] = []
    filters: List[Dict[str, str]] = []


def read_file(path: Path) -> pd.DataFrame:
    ext = path.suffix.lower()
    if ext == ".csv":
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                return pd.read_csv(path, dtype=str, keep_default_na=False, sep=None, engine="python",
                                   encoding=enc).astype(STR)
            except UnicodeDecodeError:
                continue
        raise ValueError("Cannot decode CSV")
    try:
        # calamine (Rust) is several times faster and far lighter than openpyxl
        df = pd.read_excel(path, dtype=str, keep_default_na=False, engine="calamine")
    except ImportError:
        df = pd.read_excel(path, dtype=str, keep_default_na=False, engine="pyxlsb" if ext == ".xlsb" else None)
    return df.astype(STR)


def apply_filters(df: pd.DataFrame, filters) -> pd.DataFrame:
    for f in filters:
        col, val = f.get("column", ""), str(f.get("value", "")).strip().lower()
        op = re.sub(r"[\s_-]", "", f.get("operator", "contains").lower())
        if col not in df.columns or not val:
            continue
        s = df[col].astype(STR).str.lower()
        if op in ("equals", "equal", "="):
            df = df[s == val]
        elif op in ("startswith", "starts"):
            df = df[s.str.startswith(val)]
        elif op in ("endswith", "ends"):
            df = df[s.str.endswith(val)]
        else:
            df = df[s.str.contains(val, regex=False)]
    return df


@app.get("/")
async def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/health")
async def health():
    return {"status": "healthy", "files": len(S.files), "merged": S.df is not None}


# Endpoints below are plain `def` so FastAPI runs them in a worker thread:
# a long merge/export then can't freeze the server (and fail Render's health check).
@app.post("/upload")
def upload(files: List[UploadFile] = File(...)):
    try:
        S.reset()
        for i, f in enumerate(files):
            name = re.sub(r"[^\w.\- ]", "_", Path(f.filename or f"file{i}").name)
            p = S.dir / f"{i}_{name}"
            with open(p, "wb") as out:
                shutil.copyfileobj(f.file, out, 1024 * 1024)
            S.files.append(p)
        return {"success": True, "file_count": len(S.files)}
    except Exception as e:
        log.exception("upload")
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


@app.post("/merge")
def merge():
    try:
        if not S.files:
            raise ValueError("No files uploaded")
        S.df = None
        frames, notes = [], []
        for p in S.files:
            try:
                df = read_file(p)
                df.columns = [str(c).strip() for c in df.columns]
                df = df.loc[:, ~df.columns.duplicated()]
                frames.append(df)
                notes.append(f"{p.name.split('_', 1)[1]}: {len(df)} rows")
            except Exception as e:
                notes.append(f"{p.name.split('_', 1)[1]}: FAILED ({e})")
        if not frames:
            raise ValueError("No readable data found. " + "; ".join(notes))
        S.df = pd.concat(frames, ignore_index=True, sort=False).fillna("")
        del frames
        gc.collect()
        log.info("merged %d rows", len(S.df))
        return {"success": True, "row_count": len(S.df), "columns": list(S.df.columns), "notes": notes}
    except Exception as e:
        log.exception("merge")
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


def write_xlsx(df: pd.DataFrame, out: Path, chunk: int = 100000) -> int:
    """Stream rows straight to disk with xlsxwriter (constant memory)."""
    import xlsxwriter
    cols = list(df.columns)
    n_sheets = max(1, -(-len(df) // chunk))
    wb = xlsxwriter.Workbook(str(out), {"constant_memory": True})
    ws = wb.add_worksheet("Summary")
    for r, row in enumerate([("Item", "Value"), ("Total Rows", len(df)), ("Total Columns", len(cols)),
                             ("Sheets", n_sheets), ("Export Date", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))]):
        ws.write_row(r, 0, row)
    for s in range(n_sheets):
        ws = wb.add_worksheet(f"Data_{s + 1}" if n_sheets > 1 else "Data")
        ws.write_row(0, 0, cols)
        part = df.iloc[s * chunk:(s + 1) * chunk]
        write = ws.write_string
        for r, row in enumerate(zip(*(part[c].tolist() for c in cols)), 1):
            for c, v in enumerate(row):
                if v:
                    write(r, c, v)
    wb.close()
    return n_sheets


@app.post("/export")
def export(cfg: ExportConfig):
    try:
        if S.df is None:
            raise ValueError("Merge files first")
        df = apply_filters(S.df, cfg.filters)
        cols = [c for c in (cfg.columns or list(df.columns)) if c in df.columns]
        df = df[cols]
        out = S.dir / "export.xlsx"
        write_xlsx(df, out)
        return FileResponse(out, filename=f"RFS_Merged_{datetime.now():%Y%m%d_%H%M%S}.xlsx",
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
