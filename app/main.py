from app.server import app
import os
import uvicorn
from app.routers import health, sync, immich, config
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.include_router(
    health.router,
    prefix="/health",
    tags=["health"],
    responses={404: {"description": "Not Found"}},
)
app.include_router(
    sync.router,
    prefix="/sync",
    tags=["sync"],
    responses={404: {"description": "Not Found"}},
)
app.include_router(
    immich.router,
    prefix="/immich",
    tags=["immich"],
    responses={404: {"description": "Not Found"}},
)
app.include_router(
    config.router,
    prefix="/config",
    tags=["config"],
    responses={404: {"description": "Not Found"}},
)


@app.get("/ui", include_in_schema=False)
def config_editor():
    return FileResponse(os.path.join(STATIC_DIR, "editor.html"))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)