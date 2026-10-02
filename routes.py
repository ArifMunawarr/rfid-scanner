import json
from datetime import datetime
from fastapi import APIRouter
from rfid import tags, send_command, get_connection_status, start_scan, stop_scan

router = APIRouter()


def build_tags_data():
    result = []
    i = 1
    for tag in tags.values():
        if tag["remove"]:
            continue
        dt = datetime.fromtimestamp(tag["last_time"])
        timestr = dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        result.append({
            "num": i,
            "id": tag["id"],
            "device_id": tag["device_id"],
            "epc": tag.get("epc"),
            "tid": tag.get("tid"),
            "remove": tag["remove"],
            "power": tag["power"],
            "battery": tag["battery"],
            "raw": tag.get("raw"),
            "count": tag["count"],
            "last_time": timestr
        })
        i += 1
    return {"total": len(result), "tags": result}


@router.post("/start")
def start():
    sent = start_scan()
    status = get_connection_status()
    return {"status": "start" if sent else "not_connected", "connection": status}


@router.post("/stop")
def stop():
    sent = stop_scan()
    status = get_connection_status()
    return {"status": "stop" if sent else "not_connected", "connection": status}


@router.get("/view")
def view():
    data = build_tags_data()
    data["connection"] = get_connection_status()
    return data


@router.get("/status")
def status():
    return get_connection_status()
