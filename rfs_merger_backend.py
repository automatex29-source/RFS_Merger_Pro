#!/usr/bin/env python3
"""RFS Merger Pro - local backend. Serves the UI at http://localhost:8000 and does the heavy merging."""
import io, os, re, sys, shutil, socket, tempfile, threading, time, traceback, logging, webbrowser
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
from fastapi.responses import FileResponse, JSONResponse
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
                return pd.read_csv(path, dtype=str, keep_default_na=False, sep=None, engine="python", encoding=enc)
            except UnicodeDecodeError:
                continue
        raise ValueError("Cannot decode CSV")
    engine = "pyxlsb" if ext == ".xlsb" else None
    return pd.read_excel(path, dtype=str, keep_default_na=False, engine=engine)


def apply_filters(df: pd.DataFrame, filters) -> pd.DataFrame:
    for f in filters:
        col, val = f.get("column", ""), str(f.get("value", "")).strip().lower()
        op = re.sub(r"[\s_-]", "", f.get("operator", "contains").lower())
        if col not in df.columns or not val:
            continue
        s = df[col].astype(str).str.lower()
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


@app.post("/upload")
async def upload(files: List[UploadFile] = File(...)):
    try:
        S.reset()
        for i, f in enumerate(files):
            name = re.sub(r"[^\w.\- ]", "_", Path(f.filename or f"file{i}").name)
            p = S.dir / f"{i}_{name}"
            p.write_bytes(await f.read())
            S.files.append(p)
        return {"success": True, "file_count": len(S.files)}
    except Exception as e:
        log.exception("upload")
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


@app.post("/merge")
async def merge():
    try:
        if not S.files:
            raise ValueError("No files uploaded")
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
        log.info("merged %d rows", len(S.df))
        return {"success": True, "row_count": len(S.df), "columns": list(S.df.columns), "notes": notes}
    except Exception as e:
        log.exception("merge")
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


@app.post("/export")
async def export(cfg: ExportConfig):
    try:
        if S.df is None:
            raise ValueError("Merge files first")
        df = apply_filters(S.df, cfg.filters)
        cols = [c for c in (cfg.columns or list(df.columns)) if c in df.columns]
        df = df[cols]
        out = S.dir / "export.xlsx"
        chunk = 100000
        parts = [df.iloc[i:i + chunk] for i in range(0, len(df), chunk)] or [df]
        with pd.ExcelWriter(out, engine="openpyxl") as w:
            pd.DataFrame({"Item": ["Total Rows", "Total Columns", "Sheets", "Export Date"],
                          "Value": [len(df), len(cols), len(parts), datetime.now().strftime("%Y-%m-%d %H:%M:%S")]}
                         ).to_excel(w, sheet_name="Summary", index=False)
            for i, part in enumerate(parts, 1):
                part.to_excel(w, sheet_name=f"Data_{i}" if len(parts) > 1 else "Data", index=False)
        return FileResponse(out, filename=f"RFS_Merged_{datetime.now():%Y%m%d_%H%M%S}.xlsx",
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:
        log.exception("export")
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


@app.post("/reset")
async def reset():
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
