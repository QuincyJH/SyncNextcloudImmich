from app.services import immich_service
import logging
from fastapi import APIRouter

router = APIRouter()

@router.post("/", response_model=str)
def convert_album_to_tag():
    immich_service.convert_album_to_tag(dry_run=False)
    return "album-to-tag started"