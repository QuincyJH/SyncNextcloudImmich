#!/usr/bin/env python3
import os
import sys
import json
import time
import logging
import requests
import psycopg

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger('healthcheck')

CONFIG_FILE = os.environ.get('CONFIG_FILE', '/config/user_config.json')
IMMICH_TIMEOUT = int(os.environ.get('IMMICH_TIMEOUT', '10'))
DB_HOST = os.environ.get('NEXTCLOUD_DB_HOST')
DB_PORT = int(os.environ.get('NEXTCLOUD_DB_PORT', '5432'))
DB_NAME = os.environ.get('NEXTCLOUD_DB_NAME')
DB_USER = os.environ.get('NEXTCLOUD_DB_USER')
DB_PASSWORD = os.environ.get('NEXTCLOUD_DB_PASSWORD')


def check_immich_for_users():
    if not os.path.isfile(CONFIG_FILE):
        log.warning(f'Config file not found: {CONFIG_FILE}')
        return False
    ok = True
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        users = json.load(f)
    for u in users:
        immich_url = u.get('immich_url')
        token = u.get('immich_token')
        try:
            r = requests.get(f"{immich_url}/api/albums", headers={"x-api-key": token}, timeout=IMMICH_TIMEOUT)
            if r.status_code == 200:
                log.info(f"Immich OK for user at {immich_url}")
            else:
                log.error(f"Immich FAILED ({r.status_code}) for {immich_url}: {r.text[:200]}")
                ok = False
        except Exception as e:
            log.error(f"Immich connectivity error for {immich_url}: {e}")
            ok = False
    return ok


def check_nextcloud_db():
    # Only check if credentials provided
    if not (DB_HOST and DB_NAME and DB_USER):
        log.info('DB env not set; skipping Nextcloud DB check')
        return True
    try:
        with psycopg.connect(host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, dbname=DB_NAME, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT 1;')
                row = cur.fetchone()
                if row and row[0] == 1:
                    log.info('Nextcloud DB OK')
                    return True
    except Exception as e:
        log.error(f'DB connectivity error: {e}')
    return False


def main():
    ok_immich = check_immich_for_users()
    ok_db = check_nextcloud_db()
    if ok_immich and ok_db:
        log.info('Healthcheck PASSED')
        return 0
    log.error('Healthcheck FAILED')
    return 1


if __name__ == '__main__':
    sys.exit(main())
