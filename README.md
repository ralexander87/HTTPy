# Python Upload Server

A small local file sharing server with browser uploads, command-line uploads, downloads,
and a portable single-file version.

## Setup

```bash
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Run

```bash
python -m upload_server.server --host 0.0.0.0 --port 8000
```

By default, uploaded files are stored in the directory where you start the server.
The startup output prints local URLs you can open from this computer or another
device on the same network.

You can also run the standalone file without installing the package:

```bash
python upload_server_standalone.py
```

## Browser Uploads

Open the printed URL in a browser. The page includes:

- file list
- drag-and-drop upload
- file picker upload
- download links
- `Download ZIP` for the whole shared directory

Existing files are not overwritten by default. If `photo.jpg` already exists, the
next upload is saved as `photo-1.jpg`.

## Upload From Another Machine

```bash
curl -T myfile.txt http://SERVER_IP:8000/myfile.txt
```

The server also serves the uploaded files back over `GET`, so this works too:

```bash
curl http://SERVER_IP:8000/myfile.txt
```

PowerShell upload:

```powershell
Invoke-WebRequest -Uri "http://SERVER_IP:8000/myfile.txt" -Method PUT -InFile "C:\Path\To\myfile.txt"
```

## Useful Options

Overwrite existing files:

```bash
python upload_server_standalone.py --overwrite
```

Limit each upload:

```bash
python upload_server_standalone.py --max-size 500MB
```

Stop automatically after a short sharing session:

```bash
python upload_server_standalone.py --stop-after 30m
```

Choose a different directory:

```bash
python upload_server_standalone.py --upload-dir /path/to/share
```

This server has no authentication. Use it on a trusted network.
