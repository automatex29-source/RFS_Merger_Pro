RFS MERGER PRO
==============
Windows: double-click START.bat   (Mac/Linux: ./start.sh)

First run creates a private virtual environment (.venv) and downloads everything
it needs automatically (needs internet once). Later runs start in seconds.
Your browser opens at http://localhost:8000 . Close the black window to stop.

Files:
  START.bat / start.sh       one-click setup + launch
  rfs_merger_backend.py      local server (merge, filter, export)
  static/index.html          the web interface
  requirements.txt           packages installed into .venv

Troubleshooting: if setup fails, delete the .venv folder and run START.bat again.
The interface also works if you just open static/index.html directly (it then merges
inside the browser, fine for small/medium files).

Deploy on Render (render.com):
  1. Push this folder to GitHub (the .venv folder is ignored - do not upload it).
  2. On Render: New + > Blueprint, pick the repo. render.yaml sets everything up.
     (Or New + > Web Service with
        Build command: pip install -r requirements.txt
        Start command: uvicorn rfs_merger_backend:app --host 0.0.0.0 --port $PORT)
  3. Open the https://....onrender.com URL Render gives you.
