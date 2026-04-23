# HistoryBites backend

FastAPI service behind the HistoryBites Android app. Runs on Railway.

This is **Step 1** of the implementation plan: a hello-world FastAPI deploy with a working `/health` endpoint and JSON logging. Postgres, Gemini, FCM, and the rest arrive in later steps.

## Run locally

Requires Python 3.12+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
uvicorn app.main:app --reload
```

Then visit http://127.0.0.1:8000/health — you should get `{"status": "ok"}`.

## Deploy to Railway

1. Install the Railway CLI (`brew install railway`) and `railway login`.
2. From this directory: `railway init` (pick a project name).
3. Link the GitHub repo via the Railway dashboard: **New → Deploy from GitHub repo → select `juragear/history-bites-backend`**. Railway will pick up `railway.toml` and deploy automatically on push to `main`.
4. Set env vars in the Railway dashboard (see below — Step 1 only needs `ENVIRONMENT` and `LOG_LEVEL`).
5. Once deployed, Railway will expose a public URL. Visit `<public-url>/health` in a browser to verify.

Subsequent pushes to `main` auto-deploy.

## Environment variables

| Name          | Required | Default       | Notes                                          |
|---------------|----------|---------------|------------------------------------------------|
| `ENVIRONMENT` | no       | `development` | Label used in logs and future config switches. |
| `LOG_LEVEL`   | no       | `INFO`        | Python logging level.                          |

More will be added in later steps (`DATABASE_URL`, `GEMINI_API_KEY`, `FIREBASE_SERVICE_ACCOUNT_JSON`, etc.). See Backend Architecture in Notion for the full list.

## Project layout

```
historybites-backend/
  pyproject.toml
  railway.toml
  .env.example
  README.md
  app/
    __init__.py
    main.py       # FastAPI app, JSON logging, /health
    config.py     # pydantic-settings
```

More modules (`db.py`, `models.py`, `generation.py`, ...) are added in later steps. See Backend Architecture in Notion for the target layout.

## Architectural context

See Notion:

- **HistoryBites** — project hub
- **Backend Architecture** — full technical spec (stack, schema, endpoints, generation flow)
- **Decisions Log** — why things are the way they are (notably **D11**: use stdlib `logging`, no custom singleton)

The repo also mirrors the Decisions Log as `DECISIONS.md` per **D19** (added in a later step).
