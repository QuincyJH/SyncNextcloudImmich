from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

tags_metadata = [
    {
        "name": "health",
        "description": "Checks service health"
    },
    {
        "name": "sync",
        "description": "Handles synchronization tasks"
    },
    {
        "name": "immich",
        "description": "Manages Immich related operations"
    }
]

def __create_fastapi_server() -> FastAPI:
    server = FastAPI(
        title="Immich Album Tag Sync",
        version="1.0.0",
        openapi_tags=tags_metadata
    )
    return server

app = __create_fastapi_server()