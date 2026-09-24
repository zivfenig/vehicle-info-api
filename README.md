# Vehicle Info API

A thin FastAPI wrapper around Encore's vehicle-info endpoint. It turns Encore's varied responses (200, 404, 422, …) into one predictable shape with a stable `error_code`, so a conversation flow can branch on it.

If this service answers, it always answers **HTTP 200**. Any non-2xx means the service itself is down or unreachable, not a lookup result.

## Endpoints

| Method | Path            | Description                     |
| ------ | --------------- | ------------------------------- |
| POST   | `/vehicle-info` | Look up a vehicle by plate      |
| GET    | `/health`       | Liveness probe                  |

### Request

```json
{ "license_plate": "1234567" }
```

Spaces, dashes and dots are stripped. Format validation is left to Encore.

### Success

```json
{
  "success": true,
  "data": {
    "license_plate": "1234567",
    "manufacturer": "Toyota",
    "model": "Corolla",
    "year": 2020,
    "color": "White",
    "display": "1234567, 2020 Toyota Corolla, White"
  }
}
```

### Error

```json
{ "success": false, "error_code": "INVALID_PLATE", "message": "...", "reason": "..." }
```

| `error_code`           | Meaning                                          |
| ---------------------- | ------------------------------------------------ |
| `VEHICLE_NOT_FOUND`    | Plate is valid but not in the registry           |
| `INVALID_PLATE`        | Encore rejected the format (`reason` explains)   |
| `UPSTREAM_BUSY`        | Encore overloaded (429 / 503) – retry shortly    |
| `UPSTREAM_TIMEOUT`     | Encore did not answer in time – retry shortly    |
| `UPSTREAM_UNAVAILABLE` | Encore unreachable – retry later                 |
| `UPSTREAM_ERROR`       | Any other Encore failure – retry later           |
| `INVALID_REQUEST`      | Malformed request body (caller bug)              |

`message` is English and meant for logs. `reason` is Encore's own text (Hebrew), passed through.

## Configuration

| Variable           | Default                    | Description                     |
| ------------------ | -------------------------- | ------------------------------- |
| `UPSTREAM_URL`     | Encore vehicle-info URL    | Upstream endpoint               |
| `UPSTREAM_TIMEOUT` | `10`                       | Upstream timeout in seconds     |
| `PORT`             | `8080`                     | Port to listen on               |

## Run locally

```bash
pip install -r requirements.txt
uvicorn main:app --port 8080
```

Or with Docker:

```bash
docker build -t vehicle-info-api .
docker run -p 8080:8080 vehicle-info-api
```

Interactive docs are at `http://localhost:8080/docs`.

## Deploy to Cloud Run

```bash
gcloud run deploy vehicle-info-api --source . --region us-central1 --allow-unauthenticated
```
