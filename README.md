# MechHub CAD Converter

A production-grade STEP → STL conversion microservice built with **FastAPI** and **CadQuery** (OpenCASCADE Python bindings).

---

## Why Python / CadQuery?

| | opencascade.js (old) | CadQuery (new) |
|---|---|---|
| Runtime | WASM in Node.js | Native OCCT in Python |
| Memory | Limited by WASM allocator | Full process memory |
| Cold starts | Slow (WASM JIT + emscripten) | Fast container warm-up |
| Reliability | MEMFS quirks, errno 44 | Stable, battle-tested |
| Cost | Firebase Functions (unpredictable) | Railway ~$5/month |

---

## API Contract

### `GET /health`
```json
{ "status": "ok", "version": "1.0.0" }
```

### `POST /convert`
- **Content-Type:** `multipart/form-data`
- **Field:** `file` — a `.step` or `.stp` file (max 100 MB)

**Response:**
```json
{
  "stl": "<base64-encoded binary STL>",
  "triangleCount": 12345,
  "boundingBox": { "x": 100.0, "y": 50.0, "z": 25.0 }
}
```
100% compatible with the existing `ConversionResult` TypeScript interface.

---

## Local Development

```bash
# Run with Docker (recommended)
docker build -t mechhub-cad-converter .
docker run -p 8080:8080 mechhub-cad-converter

# Test health
curl http://localhost:8080/health

# Test conversion
curl -X POST http://localhost:8080/convert \
  -F "file=@your_part.step" | python -m json.tool
```

---

## Deployment to Railway

```bash
# 1. Install Railway CLI
npm i -g @railway/cli

# 2. From this directory:
railway login
railway init      # creates a new project
railway up        # builds Docker image, deploys

# 3. Get your public URL
railway domain
```

Update `NEXT_PUBLIC_CONVERT_SERVICE_URL` in your Railway project's Variables panel (or in `.env.local` for local dev).

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8080` | HTTP port (set automatically by Railway) |
| `CAD_LINEAR_TOLERANCE` | `0.1` | Tessellation linear deflection (mm) — lower = more detail |
| `CAD_ANGULAR_TOLERANCE` | `0.5` | Tessellation angular deflection (rad) |
| `CORS_ORIGINS` | `mechhub.in,...` | Comma-separated allowed CORS origins |
