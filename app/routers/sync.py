from app.services import sync_service
import logging
from fastapi import APIRouter
from typing import Optional

router = APIRouter()

@router.post("/", response_model=str)
def sync_files_to_cloud(dry_run: Optional[bool] = None):
    sync_service.sync_files_to_cloud(dry_run=dry_run)
    if dry_run is True:
        return "dry run started"
    if dry_run is False:
        return "sync started"
    return "sync started (config dry_run applies)"

@router.post("/copy-tags", response_model=str)
def copy_nextcloud_tags_to_immich(dry_run: Optional[bool] = None):
    sync_service.copy_nextcloud_tags_to_immich(dry_run=dry_run)
    if dry_run is True:
        return "copy tags dry run started"
    if dry_run is False:
        return "copy tags started"
    return "copy tags started (config dry_run applies)"