#!/usr/bin/env python3
"""
API server to serve scraped data from Cloudflare R2 or local filesystem.

R2 key format: v1/{mode}/{year}/{month}/{day}.json
Local format: {mode}/data/{YYYYMMDD}/finalsummary.json
"""
from flask import Flask, jsonify
from flask_cors import CORS
import os
import json
from datetime import datetime, timedelta, timezone

# Try to import R2 storage, fall back to local if not available
try:
    from services.r2_storage import (
        download_json,
        list_dates,
        is_r2_configured,
        R2_BUCKET_NAME,
    )
    R2_AVAILABLE = True
except ImportError:
    R2_AVAILABLE = False

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# For local fallback
BASE_PATH = os.environ.get("DATA_PATH", ".")
IST = timezone(timedelta(hours=5, minutes=30))


def use_r2():
    """Check if we should use R2 storage"""
    return R2_AVAILABLE and is_r2_configured()


def _r2_key(mode, date_code):
    """Build R2 key from mode and YYYYMMDD date. Format: v1/{mode}/{year}/{month}/{day}.json"""
    year, month, day = date_code[:4], date_code[4:6], date_code[6:8]
    return f"v1/{mode}/{year}/{month}/{day}.json"


def get_data_from_r2(key: str):
    """Get data from R2"""
    data = download_json(key)
    if data is None:
        return None, 404
    return data, 200


def get_data_from_local(file_path: str):
    """Get data from local filesystem"""
    if not os.path.exists(file_path):
        return None, 404
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f), 200


@app.route("/health")
def health():
    """Health check endpoint"""
    storage = "r2" if use_r2() else "local"
    return jsonify({
        "status": "healthy",
        "storage": storage,
        "bucket": R2_BUCKET_NAME if use_r2() else None,
        "timestamp": datetime.now(IST).isoformat(),
    })


@app.route("/api/advance/<date>")
def get_advance_data(date):
    """Get advance booking data for a specific date (YYYYMMDD format)"""
    if use_r2():
        data, status = get_data_from_r2(_r2_key("advance", date))
    else:
        file_path = os.path.join(BASE_PATH, "advance", "data", date, "finalsummary.json")
        data, status = get_data_from_local(file_path)

    if data is None:
        return jsonify({"error": "Data not found for this date"}), 404
    return jsonify(data)


@app.route("/api/advance/<date>/detailed")
def get_advance_detailed(date):
    """Get detailed advance booking data for a specific date (local only)"""
    file_path = os.path.join(BASE_PATH, "advance", "data", date, "finaldetailed.json")
    data, status = get_data_from_local(file_path)

    if data is None:
        return jsonify({"error": "Data not found for this date"}), 404
    return jsonify(data)


@app.route("/api/daily/<date>")
def get_daily_data(date):
    """Get daily data for a specific date (YYYYMMDD format)"""
    if use_r2():
        data, status = get_data_from_r2(_r2_key("daily", date))
    else:
        file_path = os.path.join(BASE_PATH, "daily", "data", date, "finalsummary.json")
        data, status = get_data_from_local(file_path)

    if data is None:
        return jsonify({"error": "Data not found for this date"}), 404
    return jsonify(data)


@app.route("/api/daily/<date>/detailed")
def get_daily_detailed(date):
    """Get detailed daily data for a specific date (local only)"""
    file_path = os.path.join(BASE_PATH, "daily", "data", date, "finaldetailed.json")
    data, status = get_data_from_local(file_path)

    if data is None:
        return jsonify({"error": "Data not found for this date"}), 404
    return jsonify(data)


@app.route("/api/advance/latest")
def get_latest_advance():
    """Get the latest advance booking data"""
    if use_r2():
        dates = list_dates("advance")
        if not dates:
            return jsonify({"error": "No advance data found"}), 404
        latest_date = dates[0]
        data, _ = get_data_from_r2(_r2_key("advance", latest_date))
    else:
        advance_path = os.path.join(BASE_PATH, "advance", "data")
        if not os.path.exists(advance_path):
            return jsonify({"error": "No advance data found"}), 404
        dates = sorted([d for d in os.listdir(advance_path) if d.isdigit()], reverse=True)
        if not dates:
            return jsonify({"error": "No data available"}), 404
        latest_date = dates[0]
        file_path = os.path.join(advance_path, latest_date, "finalsummary.json")
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

    return jsonify({"date": latest_date, "data": data})


@app.route("/api/daily/latest")
def get_latest_daily():
    """Get the latest daily data"""
    if use_r2():
        dates = list_dates("daily")
        if not dates:
            return jsonify({"error": "No daily data found"}), 404
        latest_date = dates[0]
        data, _ = get_data_from_r2(_r2_key("daily", latest_date))
    else:
        daily_path = os.path.join(BASE_PATH, "daily", "data")
        if not os.path.exists(daily_path):
            return jsonify({"error": "No daily data found"}), 404
        dates = sorted([d for d in os.listdir(daily_path) if d.isdigit()], reverse=True)
        if not dates:
            return jsonify({"error": "No data available"}), 404
        latest_date = dates[0]
        file_path = os.path.join(daily_path, latest_date, "finalsummary.json")
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

    return jsonify({"date": latest_date, "data": data})


@app.route("/api/dates")
def list_available_dates():
    """List all available dates"""
    if use_r2():
        advance_dates = list_dates("advance")
        daily_dates = list_dates("daily")
    else:
        advance_path = os.path.join(BASE_PATH, "advance", "data")
        daily_path = os.path.join(BASE_PATH, "daily", "data")
        advance_dates = []
        daily_dates = []
        if os.path.exists(advance_path):
            advance_dates = sorted([d for d in os.listdir(advance_path) if d.isdigit()], reverse=True)
        if os.path.exists(daily_path):
            daily_dates = sorted([d for d in os.listdir(daily_path) if d.isdigit()], reverse=True)

    return jsonify({
        "storage": "r2" if use_r2() else "local",
        "advance": advance_dates,
        "daily": daily_dates,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    if use_r2():
        print(f"Using R2 storage: {R2_BUCKET_NAME}")
    else:
        print(f"Using local storage: {BASE_PATH}")
    app.run(host="0.0.0.0", port=port)
