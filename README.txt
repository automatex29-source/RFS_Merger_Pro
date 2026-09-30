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
