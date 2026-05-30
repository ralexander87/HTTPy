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
device on the same network. To share another folder, start the script from that
folder.

You can also run the standalone file without installing the package:

```bash
python upload_server_standalone.py
```

## Browser Uploads

Open the printed URL in a browser. The page includes:

- file list
- file picker upload
- download links
- `Download ZIP` for the whole shared directory
- selected file/folder ZIP downloads
- selected file/folder delete
- a refresh button for the file list
- editable command presets that persist after server restart
- two lightweight CLI panels that run commands in the shared directory
- live settings for upload size, rename/overwrite mode, hidden visibility, command timeout, and auto-stop

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

The browser CLI, settings, upload, download, and delete actions are always
available without authentication. Use this only for short personal sessions on
trusted networks.

Limit each upload:

```bash
python upload_server_standalone.py --max-size 500MB
```

Stop automatically after a short sharing session:

```bash
python upload_server_standalone.py --stop-after 30m
```

Limit browser CLI commands:

```bash
python upload_server_standalone.py --command-timeout 30s
```

By default, dot-hidden paths are not listed or served, directory listings
are disabled, and symlinks that escape the shared directory are blocked. Anyone
who can reach the server can still upload, download, delete, change settings,
and run CLI commands, so use it only on a trusted network.

Use the browser buttons to switch Rename/Overwrite mode or Hidden/Visible paths
while the server is running.
