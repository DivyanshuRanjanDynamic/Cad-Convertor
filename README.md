# MechHub CAD Converter

A high-performance STEP → STL conversion and CNC costing microservice built with **FastAPI** and **CadQuery** (OpenCASCADE Python bindings).

---

## Architecture & Responsibilities

| Service | Responsibility |
|---|---|
| **Studio (Next.js)** | Web UI, user auth, quote generation, and ticket minting (`/api/v1/cad/convert-ticket`). |
| **CAD Converter (FastAPI)** | High-throughput geometric processing: tessellating STEP files into binary STL, extracting CNC machinability features, and calculating cycle times via native OCCT. |

### Why Python / CadQuery?

| Metric | opencascade.js (WASM) | CadQuery / OCCT (Native) |
|---|---|---|
| **Runtime** | WASM in Node.js | Native OpenCASCADE in Python |
| **Memory** | Limited by WASM 32-bit allocator | Full container memory allocation |
| **Cold Starts** | Slow (WASM JIT compilation) | Instant warm container execution |
| **Reliability** | Virtual MEMFS quirks, errno 44 | Stable, production battle-tested |

---

## API Contract

### 1. `GET /health`
Liveness probe for orchestrators and health checks.
```json
{ "status": "ok", "version": "1.0.0" }
```

### 2. `POST /convert`
Uploads a `.step` or `.stp` file and converts it into a base64-encoded binary STL.

- **Authentication:**
  - **Browser client:** `Authorization: Bearer <ticket>` (minted by Studio at `POST /api/v1/cad/convert-ticket`, 10-minute TTL, signed with `CONVERTER_SERVICE_TOKEN`).
  - **Direct service / CLI:** `Authorization: Bearer <CONVERTER_SERVICE_TOKEN>`.
  - Unauthenticated requests are rejected immediately with `401` *before* reading the payload. If the server has no token configured, returns `503`.
- **Modes:**
  - **Asynchronous (Default):** Returns HTTP `202 Accepted` with a job ID. Poll `GET /status/{job_id}` for completion.
    ```json
    {
      "jobId": "f47ac10b58cc4372a5670e02b2c3d479",
      "status": "queued",
      "fileName": "part.step",
      "fileSize": 1048576,
      "createdAt": "2026-10-20T12:00:00Z"
    }
    ```
  - **Synchronous (`?sync=true`):** Waits for processing and returns the STL payload directly:
    ```json
    {
      "stl": "<base64-encoded binary STL>",
      "triangleCount": 12345,
      "boundingBox": { "x": 100.0, "y": 50.0, "z": 25.0 }
    }
    ```
- **Quotas (in-memory per caller):**
  - Up to `CONVERT_MAX_PER_HOUR` (default: 30) requests/hour.
  - Up to `CONVERT_MAX_ACTIVE_PER_USER` (default: 2) concurrent conversions.
  - Global concurrency ceiling of `CONVERT_MAX_ACTIVE_JOBS` (default: 6).

### 3. `GET /status/{job_id}`
Retrieves status and results for an async conversion job.
- **Pending/Processing:** `{ "status": "queued" | "processing", "progress": 50 }`
- **Completed:** Returns full conversion object (`stl`, `triangleCount`, `boundingBox`, `status: "completed"`).
- **Failed:** `{ "status": "failed", "error": "<reason>" }`

### 4. `POST /internal/analyze-cnc`
Server-to-server endpoint for secure CNC costing analysis.
- **Authentication:** `Authorization: Bearer <CONVERTER_SERVICE_TOKEN>`.
- **Body (JSON):**
  ```json
  {
    "fileUrl": "https://s3.ap-south-1.amazonaws.com/mechhub-uploads/temp-part.step?...",
    "fileName": "part.step"
  }
  ```
- **Security:** Only downloads from allowed HTTPS host suffixes (`FILE_URL_ALLOWED_HOSTS`), enforces 100 MB limits, and ignores HTTP redirects.

### 5. `POST /analyze-cnc`
Direct multipart upload for CNC costing (used for debugging/admin scripts).
- **Authentication:** `Authorization: Bearer <CONVERTER_SERVICE_TOKEN>`.
- **Field:** `file` (multipart `.step` / `.stp`).

#### CNC Analysis Output Schema
Mirrored by the TypeScript interface `CncAnalysis` in `studio/src/types/quoting.ts`:
- **`process.recommended`**: `cnc_machining_3axis`, `cnc_machining_5axis`, or `cnc_turning`.
- **`stock`**: Bounding stock dimensions, standard cut stock (round bar or sawed block), removed volume.
- **`setups`**: Minimum tool approach directions (ray-cast accessibility).
- **`features`**: Hole dimensions, depth, through-holes, minimum internal fillet radii, thin wall alerts.
- **`machiningTime`**: Per-part cycle times broken down by material group (`aluminium`, `mild_steel`, `stainless`, `brass`, `plastics`).
- **`setupTimeMin` / `programmingTimeMin`**: Fixed setup overheads per job batch.
- **`dfm`**: Rule violations (`DEEP_HOLE`, `THIN_WALL`, `UNDERCUT`, `TIGHT_TOLERANCE`).

---

## Local Development & Docker Setup

### 1. Build the Docker Image

Run from the `cad-converter` directory:

```bash
docker build -t mechhub-cad-converter .
```

### 2. Configure Shared Secret Token

A shared secret (`CONVERTER_SERVICE_TOKEN`) is required to authenticate requests between the Studio and the CAD converter.

Generate a token (e.g., using Node.js):
```bash
node -e "console.log(require('crypto').randomBytes(32).toString('base64url'))"
```

#### A. Configure Studio (`studio/.env.local`)
Add the following to [studio/.env.local](file:///c:/Users/Divyanshu/Projects/MechHub/studio/.env.local):
```env
CONVERTER_SERVICE_TOKEN=your-generated-secret-token
CONVERTER_INTERNAL_URL=http://localhost:8080
NEXT_PUBLIC_CONVERT_SERVICE_URL=http://localhost:8080/convert
```
*(Remember to restart your Next.js dev server `npm run dev` after changing `.env.local`)*.

#### B. Run the CAD Converter Container

##### Windows (PowerShell):
```powershell
docker run -d -p 8080:8080 `
  -e CONVERTER_SERVICE_TOKEN="your-generated-secret-token" `
  -e CORS_ORIGINS="http://localhost:3000,http://localhost:9002,https://mechhub.in" `
  --name mechhub-cnc-dev `
  mechhub-cad-converter
```

##### Linux / macOS (Bash):
```bash
docker run -d -p 8080:8080 \
  -e CONVERTER_SERVICE_TOKEN="your-generated-secret-token" \
  -e CORS_ORIGINS="http://localhost:3000,http://localhost:9002,https://mechhub.in" \
  --name mechhub-cnc-dev \
  mechhub-cad-converter
```

---

## Verification & Testing

### 1. Healthcheck
```bash
curl http://localhost:8080/health
```

### 2. Interactive Swagger UI
Open [http://localhost:8080/docs](http://localhost:8080/docs) in your browser. Click **Authorize** and enter your `CONVERTER_SERVICE_TOKEN` to test routes interactively.

### 3. Test Synchronous Conversion via cURL
```bash
curl -X POST "http://localhost:8080/convert?sync=true" \
  -H "Authorization: Bearer your-generated-secret-token" \
  -F "file=@sample_part.step"
```

### 4. Test CNC Analysis via cURL
```bash
curl -X POST "http://localhost:8080/analyze-cnc" \
  -H "Authorization: Bearer your-generated-secret-token" \
  -F "file=@sample_part.step"
```

### 5. Running the Test Suite
Run the 50+ unit and integration tests inside the CadQuery base environment:

```bash
docker run --rm -v "$PWD:/src:ro" -w /src --entrypoint /opt/conda/envs/cq/bin/python \
  cadquery/cadquery:latest -B -m unittest discover -s tests -v
```

Or locally if Python 3.8+ with CadQuery/OCCT is installed:
```bash
python -m unittest discover -s tests -v
```

---

## Deployment (Railway)

1. **Install Railway CLI:**
   ```bash
   npm i -g @railway/cli
   ```

2. **Initialize and Deploy:**
   ```bash
   railway login
   railway init
   railway up
   ```

3. **Set Environment Variables in Railway:**
   - In your Railway service dashboard, set:
     - `CONVERTER_SERVICE_TOKEN`: Matches the production studio's secret.
     - `CORS_ORIGINS`: Comma-separated domains (e.g. `https://mechhub.in,https://studio.mechhub.in`).
     - `FILE_URL_ALLOWED_HOSTS`: `.amazonaws.com` (or your S3 bucket endpoint).

4. **Link to Next.js Studio:**
   Update your Next.js production environment variables:
   - `NEXT_PUBLIC_CONVERT_SERVICE_URL`: `https://your-converter-domain.railway.app/convert`
   - `CONVERTER_INTERNAL_URL`: `https://your-converter-domain.railway.app`
   - `CONVERTER_SERVICE_TOKEN`: Same shared secret.

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8080` | HTTP port for Uvicorn (set automatically by Railway). |
| `CONVERTER_SERVICE_TOKEN` | *None* (**Required**) | Shared authentication secret. Must match Studio. Unset ⇒ returns `503`. |
| `CORS_ORIGINS` | `https://mechhub.in,...` | Comma-separated allowed origins for browser `/convert` requests. |
| `CAD_LINEAR_TOLERANCE` | `0.1` | Mesh linear deflection in mm (lower = higher triangle count, smoother surfaces). |
| `CAD_ANGULAR_TOLERANCE` | `0.5` | Mesh angular deflection in radians. |
| `CONVERT_MAX_PER_HOUR` | `30` | Max `/convert` requests allowed per user per hour. |
| `CONVERT_MAX_ACTIVE_PER_USER` | `2` | Max concurrent conversions allowed for a single user. |
| `CONVERT_MAX_ACTIVE_JOBS` | `6` | Global maximum concurrent conversions across all users. |
| `FILE_URL_ALLOWED_HOSTS` | `.amazonaws.com` | Allowed hostname suffixes for presigned URLs in `/internal/analyze-cnc`. |
