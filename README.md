# Python Upload Server

A small local file sharing server with browser uploads, command-line uploads, downloads,
and a portable single-file version.

## Security Warning

This tool is intentionally lightweight and does not include authentication.
Anyone who can reach the server can upload, download, delete, change settings,
and run CLI commands.

Use only on trusted networks, do not expose it directly to the public internet,
and stop the server when you are done.

See [SECURITY.md](SECURITY.md) for usage expectations and reporting details.

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

  <div align="center">
  <table border="0" cellspacing="0" cellpadding="5">
    <tr>
      <td align="center">
        <a href="https://raw.githubusercontent.com/ralexander87/HTTPy/main/screenshot/screenshot_20260530_205258.jpg"><img src="https://raw.githubusercontent.com/ralexander87/HTTPy/main/screenshot/screenshot_20260530_205258.jpg" width="250" height="150" alt="Image 1" style="object-fit: cover;"></a>
      </td>
      <td align="center">
        <a href="https://raw.githubusercontent.com/ralexander87/HTTPy/main/screenshot/screenshot_20260530_205341.jpg"><img src="https://raw.githubusercontent.com/ralexander87/HTTPy/main/screenshot/screenshot_20260530_205341.jpg" width="250" height="150" alt="Image 2" style="object-fit: cover;"></a>
      </td>
      <td align="center">
        <a href="https://raw.githubusercontent.com/ralexander87/HTTPy/main/screenshot/screenshot_20260530_205429.jpg"><img src="https://raw.githubusercontent.com/ralexander87/HTTPy/main/screenshot/screenshot_20260530_205429.jpg" width="250" height="150" alt="Image 3" style="object-fit: cover;"></a>
      </td>
    </tr>
  </table>
</div>

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
- live toggle buttons for rename/overwrite mode, hidden visibility, and logging
- a local `.upload_server.log` file for server start/stop, uploads, downloads, deletes, commands, setting changes, and rejected requests, with a browser toggle to pause logging

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

Set host and port at startup:

```bash
python upload_server_standalone.py --host 127.0.0.1 --port 9000
```

By default, dot-hidden paths are not listed or served, directory listings
are disabled, and symlinks that escape the shared directory are blocked. Anyone
who can reach the server can still upload, download, delete, change settings,
and run CLI commands, so use it only on a trusted network.

Use the browser buttons to switch Rename/Overwrite mode, Hidden/Visible paths,
or Log/No Log while the server is running.

The server creates `.upload_server.log` in the directory where it starts. It is
hidden from the normal browser file list, but keeps useful local activity
history for short sharing sessions.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
