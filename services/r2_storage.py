#!/usr/bin/env python3
"""
R2 Storage Utility - Handles all Cloudflare R2 operations
Uses S3-compatible API via boto3
"""
import os
import json
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# R2 Configuration from environment variables
R2_ENDPOINT = os.environ.get("R2_ENDPOINT")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY")
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "cinebotrends-data")
R2_KEY_PREFIX = os.environ.get("R2_KEY_PREFIX", "")


def _full_key(key: str) -> str:
    """Prepend R2_KEY_PREFIX to a key if set."""
    if R2_KEY_PREFIX:
        return f"{R2_KEY_PREFIX.rstrip('/')}/{key.lstrip('/')}"
    return key


def get_r2_client():
    """Create and return an R2 client"""
    if not all([R2_ENDPOINT, R2_ACCESS_KEY, R2_SECRET_KEY]):
        raise ValueError(
            "R2 credentials not configured. Set R2_ENDPOINT, R2_ACCESS_KEY, R2_SECRET_KEY"
        )

    return boto3.client(
        's3',
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        config=Config(
            signature_version='s3v4',
            retries={'max_attempts': 3, 'mode': 'adaptive'}
        )
    )

def upload_json(key: str, data: dict) -> bool:
    """
    Upload JSON data to R2

    Args:
        key: The object key (path in bucket)
        data: Dictionary to upload as JSON

    Returns:
        True if successful, False otherwise
    """
    full = _full_key(key)
    try:
        client = get_r2_client()
        json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')

        client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=full,
            Body=json_bytes,
            ContentType='application/json'
        )
        print(f"Uploaded to R2: {full}")
        return True
    except Exception as e:
        print(f"Failed to upload {full} to R2: {e}")
        return False

def upload_file(local_path: str, key: str) -> bool:
    """
    Upload a local file to R2

    Args:
        local_path: Path to the local file
        key: The object key (path in bucket)

    Returns:
        True if successful, False otherwise
    """
    full = _full_key(key)
    try:
        client = get_r2_client()

        with open(local_path, 'rb') as f:
            client.put_object(
                Bucket=R2_BUCKET_NAME,
                Key=full,
                Body=f.read(),
                ContentType='application/json'
            )
        print(f"Uploaded to R2: {full}")
        return True
    except Exception as e:
        print(f"Failed to upload {full} to R2: {e}")
        return False

def download_json(key: str) -> dict | None:
    """
    Download JSON data from R2

    Args:
        key: The object key (path in bucket)

    Returns:
        Parsed JSON as dict, or None if not found
    """
    full = _full_key(key)
    try:
        client = get_r2_client()
        response = client.get_object(Bucket=R2_BUCKET_NAME, Key=full)
        content = response['Body'].read().decode('utf-8')
        return json.loads(content)
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            return None
        raise
    except Exception as e:
        print(f"Failed to download {full} from R2: {e}")
        return None

def list_keys(prefix: str) -> list[str]:
    """
    List all keys under a prefix

    Args:
        prefix: The prefix to filter by

    Returns:
        List of keys
    """
    full_prefix = _full_key(prefix)
    try:
        client = get_r2_client()
        keys = []

        paginator = client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=full_prefix):
            for obj in page.get('Contents', []):
                keys.append(obj['Key'])

        return keys
    except Exception as e:
        print(f"Failed to list keys with prefix {full_prefix}: {e}")
        return []

def list_dates(data_type: str = "advance") -> list[str]:
    """
    List all available dates for a data type.

    R2 key format: v1/{mode}/{year}/{month}/{day}.json
    Returns dates as YYYYMMDD strings, sorted descending.
    """
    prefix = f"v1/{data_type}/"
    keys = list_keys(prefix)

    # Extract dates from keys like: tt_gross/v1/daily/2026/03/13.json
    full_prefix = _full_key(prefix)
    dates = set()
    for key in keys:
        remainder = key.replace(full_prefix, "")  # e.g. "2026/03/13.json"
        parts = remainder.split("/")
        if len(parts) == 3:
            year, month, day_file = parts
            day = day_file.replace(".json", "")
            if year.isdigit() and month.isdigit() and day.isdigit():
                dates.add(f"{year}{month}{day}")

    return sorted(dates, reverse=True)

def key_exists(key: str) -> bool:
    """Check if a key exists in R2"""
    full = _full_key(key)
    try:
        client = get_r2_client()
        client.head_object(Bucket=R2_BUCKET_NAME, Key=full)
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == '404':
            return False
        raise
    except:
        return False

def is_r2_configured() -> bool:
    """Check if R2 is properly configured"""
    return all([R2_ENDPOINT, R2_ACCESS_KEY, R2_SECRET_KEY])

# Quick test function
if __name__ == "__main__":
    if is_r2_configured():
        print(f"R2 configured for bucket: {R2_BUCKET_NAME}")
        print(f"   Endpoint: {R2_ENDPOINT}")
        print(f"   Key prefix: {R2_KEY_PREFIX or '(none)'}")

        # Test listing
        advance_dates = list_dates("advance")
        daily_dates = list_dates("daily")
        print(f"   Advance dates: {len(advance_dates)}")
        print(f"   Daily dates: {len(daily_dates)}")
    else:
        print("R2 not fully configured. Missing environment variables.")
