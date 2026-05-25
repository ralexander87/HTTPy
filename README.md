# Python Upload Server

A small Python project for experimenting with HTTP file uploads.

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

## Upload From Another Machine

```bash
curl -T myfile.txt http://SERVER_IP:8000/myfile.txt
```

The server also serves the uploaded files back over `GET`, so this works too:

```bash
curl http://SERVER_IP:8000/myfile.txt
```

This starter server has no authentication yet. Use it only on a trusted network until we add access control.
