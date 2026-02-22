from app.server import app
import uvicorn
from app.routers import health, sync, immich
from fastapi.middleware.cors import CORSMiddleware

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
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)