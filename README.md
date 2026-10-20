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

### `POST /convert` (signed upload ticket)
- **Auth:** `Authorization: Bearer <ticket>`. The studio issues tickets to signed-in, verified users at
  `POST /api/v1/cad/convert-ticket` (10-minute life), signed with `CONVERTER_SERVICE_TOKEN`
  (`v1.<base64url JSON {sub, scope:"convert", exp}>.<base64url HMAC-SHA256>`; see `convert_access.py`).
  The service token itself is also accepted. Missing/invalid ticket ⇒ `401` **before the upload is read**;
  no token configured ⇒ `503`.
- **Quotas (per user, in memory):** `CONVERT_MAX_PER_HOUR`, `CONVERT_MAX_ACTIVE_PER_USER`, and a global
  `CONVERT_MAX_ACTIVE_JOBS` cap ⇒ `429` / `503` when exceeded. Applies to `?sync=true` too.
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

### `POST /internal/analyze-cnc` (service token)
- **Auth:** `Authorization: Bearer $CONVERTER_SERVICE_TOKEN`. Without it: `401`; with no token configured: `503`.
- **Body (JSON):** `{ "fileUrl": "<presigned S3 GET URL>", "fileName": "part.step" }`
- Called only by the studio **server** (`studio/src/lib/converter-client.ts`). The browser uploads the
  STEP file to S3; the studio server presigns a 10-minute GET URL and calls here. `fileUrl` must be
  `https` on a host in `FILE_URL_ALLOWED_HOSTS`; redirects are not followed; max 100 MB.
- The studio stores the result (`cncAnalyses/{analysisId}`) and CNC quotes reference that id, so a
  customer can never submit their own machining times.

### `POST /analyze-cnc` (service token)
- **Auth:** same bearer token as above (for ops / curl testing; browsers no longer call it).
- **Content-Type:** `multipart/form-data`
- **Field:** `file`: a `.step` or `.stp` file (max 100 MB)
- Synchronous; typically 10–100 ms for ordinary parts.

Both return the same analysis:

Material-agnostic CNC costing analysis, mirrored by the `CncAnalysis` TypeScript type
and priced by `generateCncQuote()` in `studio/src/lib/quoting/pricing-engine.ts`:

| Field | Meaning |
|---|---|
| `process.recommended` | `cnc_machining_3axis`, `cnc_machining_5axis` or `cnc_turning` (+ `secondaryMilling`) |
| `stock` | Standard block (plate thickness + 5 mm saw steps) or round bar, with `removedVolumeMm3` |
| `setups` | Minimum 3-axis approach directions (ray-cast accessibility), or lathe ops |
| `features` | Merged holes (Ø, depth, through), min internal radius, min wall, sharp internal corners |
| `machiningTime` | Per-part cycle breakdown for each machinability group (`aluminium`, `stainless`, …) |
| `setupTimeMin` / `programmingTimeMin` | One-time per batch, amortised over quantity in pricing |
| `dfm` | Issues in the studio `DFMIssue` shape (`UNDERCUT`, `DEEP_HOLE`, `THIN_WALL`, …) |

Geometry extraction lives in `cnc_analysis.py` (OCCT). The time and stock model is in
`cnc_model.py` and is pure Python. Its rate tables (`MACHINABILITY`, setup constants)
are the knobs to calibrate against real job cards.

### Tests

```bash
# Pure model + OCCT integration tests inside the production image
docker run --rm -v "$PWD:/src:ro" -w /src --entrypoint /opt/conda/envs/cq/bin/python \
  cadquery/cadquery:latest -B -m unittest discover -s tests -v
```

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
| `CORS_ORIGINS` | `mechhub.in,...` | Comma-separated allowed CORS origins (browser `/convert` previews) |
| `CONVERTER_SERVICE_TOKEN` | — (**required**) | Shared secret for `/analyze-cnc`, `/internal/analyze-cnc`, and the key that signs `/convert` tickets; must match the studio's `CONVERTER_SERVICE_TOKEN`. Unset ⇒ those endpoints return 503 |
| `CONVERT_MAX_PER_HOUR` | `30` | `/convert` uploads per user per hour |
| `CONVERT_MAX_ACTIVE_PER_USER` | `2` | Conversions one user may have running at once |
| `CONVERT_MAX_ACTIVE_JOBS` | `6` | Conversions running at once across all users |
| `FILE_URL_ALLOWED_HOSTS` | `.amazonaws.com` | Comma-separated host suffixes `/internal/analyze-cnc` may download from |
