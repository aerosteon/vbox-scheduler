from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os
import time
import urllib.parse
from html import unescape

from fastapi import FastAPI, Form, Query
from fastapi.requests import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

import requests
import xmltodict


app = FastAPI(title="VBox Scheduler")

app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

VBOX_BASE_URL = os.getenv("VBOX_BASE_URL")
if not VBOX_BASE_URL:
    raise RuntimeError("VBOX_BASE_URL environment variable is required")

LOCAL_TZ = ZoneInfo(os.getenv("TZ", "Europe/London"))
PX_PER_MINUTE = 5
CHANNEL_COL_WIDTH = 170
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "300"))

_cache = {
    "channels": {"time": 0, "data": None},
    "xmltv": {"time": 0, "data": None},
}


def vbox_api(url: str) -> str:
    response = requests.get(url, verify=True, timeout=60)
    response.raise_for_status()
    return response.content.decode("utf-8-sig", errors="replace").lstrip()


def to_vbox_time(value: str) -> str:
    dt = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.strftime("%Y%m%d%H%M%S %z")


def parse_vbox_time(value: str):
    if not value:
        return None

    for fmt in ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M%S"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=LOCAL_TZ)
            return dt.astimezone(LOCAL_TZ)
        except Exception:
            pass

    return None


def format_vbox_time(value: str) -> str:
    dt = parse_vbox_time(value)
    if dt:
        return dt.strftime("%d %b %Y, %H:%M")
    return value


def html_datetime(value: str) -> str:
    dt = parse_vbox_time(value)
    if dt:
        return dt.strftime("%Y-%m-%dT%H:%M")
    return ""


def get_text(value, default="Unknown") -> str:
    if isinstance(value, dict):
        return value.get("#text", default)

    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict):
            return first.get("#text", default)
        return str(first)

    return value or default


def get_title(value) -> str:
    return get_text(value, "Unknown")


def clean_channel_label(value: str) -> str:
    value = value or ""

    value = urllib.parse.unquote(value)
    value = unescape(value)
    value = urllib.parse.unquote(value)
    value = unescape(value)

    if value.startswith("www."):
        value = value[4:]

    if value.endswith(".co.un"):
        value = value[:-6]

    value = value.replace("; ", " ")
    value = value.replace(";", "")

    return value or "Unknown"


def get_channel_name(channel) -> str:
    name = get_text(channel.get("display-name"), "")

    if not name or name.startswith("www."):
        return clean_channel_label(channel.get("@id", ""))

    return clean_channel_label(name)


def normalise_title(value: str) -> str:
    return " ".join((value or "").lower().split())


def get_channels():
    now = time.time()

    if _cache["channels"]["data"] is not None and now - _cache["channels"]["time"] < CACHE_TTL_SECONDS:
        return _cache["channels"]["data"]

    xml = vbox_api(
        f"{VBOX_BASE_URL}/cgi-bin/HttpControl/HttpControlApp"
        f"?OPTION=1&Method=GetXmltvChannelsList&FromChIndex=1&ToChIndex=500"
    )

    data = xmltodict.parse(xml)
    tv = data.get("tv") or {}
    channels = tv.get("channel", [])

    if isinstance(channels, dict):
        channels = [channels]

    cleaned = []

    for channel in channels:
        cleaned.append(
            {
                "id": channel.get("@id", ""),
                "name": get_channel_name(channel),
            }
        )

    _cache["channels"]["time"] = now
    _cache["channels"]["data"] = cleaned

    return cleaned


def get_recordings():
    xml = vbox_api(
        f"{VBOX_BASE_URL}/cgi-bin/HttpControl/HttpControlApp"
        f"?OPTION=1&Method=GetRecordsList"
    )

    data = xmltodict.parse(xml)
    record_list = data.get("record-list") or {}
    records = record_list.get("record", [])

    if isinstance(records, dict):
        records = [records]

    cleaned = []

    for record in records:
        state = record.get("state", "unknown")

        cleaned.append(
            {
                "id": record.get("record-id", ""),
                "title": get_title(record.get("programme-title")),
                "channel": record.get("channel-name", "Unknown"),
                "start_raw": record.get("@start", ""),
                "end_raw": record.get("@stop", ""),
                "start": format_vbox_time(record.get("@start", "")),
                "end": format_vbox_time(record.get("@stop", "")),
                "state": state,
            }
        )

    return cleaned


def build_scheduled_lookup(recordings):
    lookup = {}

    for record in recordings:
        if record["state"] not in ["scheduled", "recording"]:
            continue

        start_dt = parse_vbox_time(record.get("start_raw", ""))
        if not start_dt:
            continue

        title = normalise_title(record.get("title", ""))
        start_key = start_dt.strftime("%Y%m%d%H%M")

        lookup[(title, start_key)] = record.get("id", "")

    return lookup


def group_recordings(recordings):
    return {
        "recording": [r for r in recordings if r["state"] == "recording"],
        "scheduled": [r for r in recordings if r["state"] == "scheduled"],
        "recorded": [r for r in recordings if r["state"] == "recorded"],
        "other": [
            r
            for r in recordings
            if r["state"] not in ["recording", "scheduled", "recorded"]
        ],
    }


def schedule_recording(channel_id: str, title: str, start_time: str, end_time: str):
    start = to_vbox_time(start_time)
    end = to_vbox_time(end_time)

    url = (
        f"{VBOX_BASE_URL}/cgi-bin/HttpControl/HttpControlApp"
        f"?OPTION=1"
        f"&Method=ScheduleChannelRecord"
        f"&StartTime={urllib.parse.quote(start)}"
        f"&EndTime={urllib.parse.quote(end)}"
        f"&ChannelID={urllib.parse.quote(channel_id, safe='%')}"
        f"&ProgramName={urllib.parse.quote(title)}"
    )

    vbox_api(url)


def cancel_recording(record_id: str):
    url = (
        f"{VBOX_BASE_URL}/cgi-bin/HttpControl/HttpControlApp"
        f"?OPTION=1"
        f"&Method=CancelRecord"
        f"&RecordID={urllib.parse.quote(record_id)}"
    )

    vbox_api(url)


def get_vbox_matched_xmltv():
    now = time.time()

    if _cache["xmltv"]["data"] is not None and now - _cache["xmltv"]["time"] < CACHE_TTL_SECONDS:
        return _cache["xmltv"]["data"]

    xml = vbox_api(
        f"{VBOX_BASE_URL}/cgi-bin/HttpControl/HttpControlApp"
        f"?OPTION=1&Method=GetXmltvEntireFile&ExternalIP=127.0.0.1&Port=55555"
    )

    _cache["xmltv"]["time"] = now
    _cache["xmltv"]["data"] = xml

    return xml


def build_channel_map(tv):
    xmltv_channels = tv.get("channel", [])

    if isinstance(xmltv_channels, dict):
        xmltv_channels = [xmltv_channels]

    channel_map = {}

    for channel in xmltv_channels:
        channel_id = channel.get("@id", "")
        name = get_text(channel.get("display-name"), "")

        if not name:
            name = channel_id

        name = clean_channel_label(name)

        if channel_id:
            channel_map[channel_id] = name

    return channel_map


def round_down_to_half_hour(dt: datetime):
    minute = 0 if dt.minute < 30 else 30
    return dt.replace(minute=minute, second=0, microsecond=0)


def get_epg(channel_id: str = "", hours: int = 12, scheduled_lookup=None):
    if scheduled_lookup is None:
        scheduled_lookup = {}

    try:
        now = datetime.now(LOCAL_TZ)
        window_start = round_down_to_half_hour(now)
        window_end = window_start + timedelta(hours=hours)

        now_line_px = None
        if window_start <= now <= window_end:
            now_line_px = int((now - window_start).total_seconds() / 60 * PX_PER_MINUTE)

        xml = get_vbox_matched_xmltv()
        data = xmltodict.parse(xml)
        tv = data.get("tv") or {}

        programmes = tv.get("programme", [])
        if isinstance(programmes, dict):
            programmes = [programmes]

        channel_names = build_channel_map(tv)
        vbox_channels = get_channels()
        vbox_channel_ids = [c["id"] for c in vbox_channels]

        rows_by_id = {}

        for programme in programmes:
            programme_channel_id = programme.get("@channel", "")

            if channel_id and programme_channel_id != channel_id:
                continue

            start_raw = programme.get("@start", "")
            stop_raw = programme.get("@stop", "")

            start_dt = parse_vbox_time(start_raw)
            stop_dt = parse_vbox_time(stop_raw)

            if not start_dt or not stop_dt:
                continue

            if stop_dt <= window_start or start_dt >= window_end:
                continue

            clipped_start = max(start_dt, window_start)
            clipped_stop = min(stop_dt, window_end)

            left_minutes = int((clipped_start - window_start).total_seconds() / 60)
            duration_minutes = max(10, int((clipped_stop - clipped_start).total_seconds() / 60))

            title = get_title(programme.get("title"))
            start_key = start_dt.strftime("%Y%m%d%H%M")
            scheduled_record_id = scheduled_lookup.get((normalise_title(title), start_key), "")

            programme_item = {
                "channel_id": programme_channel_id,
                "title": title,
                "desc": get_text(programme.get("desc"), ""),
                "time": f"{start_dt.strftime('%H:%M')} - {stop_dt.strftime('%H:%M')}",
                "start_input": html_datetime(start_raw),
                "stop_input": html_datetime(stop_raw),
                "left_px": left_minutes * PX_PER_MINUTE,
                "width_px": duration_minutes * PX_PER_MINUTE,
                "is_scheduled": bool(scheduled_record_id),
                "record_id": scheduled_record_id,
            }

            if programme_channel_id not in rows_by_id:
                rows_by_id[programme_channel_id] = {
                    "id": programme_channel_id,
                    "name": channel_names.get(
                        programme_channel_id,
                        channel_names.get(unescape(programme_channel_id), clean_channel_label(programme_channel_id)),
                    ),
                    "programmes": [],
                }

            rows_by_id[programme_channel_id]["programmes"].append(programme_item)

        rows = []

        for ch_id in vbox_channel_ids:
            if ch_id in rows_by_id:
                rows.append(rows_by_id[ch_id])

        for ch_id, row in rows_by_id.items():
            if ch_id not in vbox_channel_ids:
                rows.append(row)

        time_slots = []
        slot = window_start

        while slot <= window_end:
            minutes_from_start = int((slot - window_start).total_seconds() / 60)
            time_slots.append(
                {
                    "label": slot.strftime("%H:%M"),
                    "left_px": minutes_from_start * PX_PER_MINUTE,
                }
            )
            slot += timedelta(minutes=30)

        return {
            "method": "VBox matched XMLTV: GetXmltvEntireFile",
            "rows": rows,
            "time_slots": time_slots,
            "timeline_width": int(hours * 60 * PX_PER_MINUTE),
            "channel_col_width": CHANNEL_COL_WIDTH,
            "now_line_px": now_line_px,
            "now_label": now.strftime("%H:%M"),
            "window_label": f"{window_start.strftime('%a %d %b %H:%M')} - {window_end.strftime('%H:%M')}",
            "error": "",
        }

    except Exception as exc:
        return {
            "method": "",
            "rows": [],
            "time_slots": [],
            "timeline_width": 0,
            "channel_col_width": CHANNEL_COL_WIDTH,
            "now_line_px": None,
            "now_label": "",
            "window_label": "",
            "error": f"Could not load EPG: {exc}",
        }


@app.get("/", response_class=HTMLResponse)
def epg(
    request: Request,
    channel_id: str = Query("", alias="channel"),
    hours: int = Query(12),
):
    recordings = get_recordings()
    scheduled_lookup = build_scheduled_lookup(recordings)

    channels = get_channels()
    epg_data = get_epg(
        channel_id=channel_id,
        hours=hours,
        scheduled_lookup=scheduled_lookup,
    )

    return templates.TemplateResponse(
        request,
        "epg.html",
        {
            "channels": channels,
            "selected_channel": channel_id,
            "hours": hours,
            "epg": epg_data,
            "vbox_base_url": VBOX_BASE_URL,
        },
    )


@app.get("/schedule", response_class=HTMLResponse)
def schedule_page(request: Request):
    recordings = get_recordings()

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "channels": get_channels(),
            "groups": group_recordings(recordings),
            "vbox_base_url": VBOX_BASE_URL,
        },
    )


@app.post("/schedule")
def schedule(
    channel_id: str = Form(...),
    title: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
):
    schedule_recording(channel_id, title, start_time, end_time)
    return RedirectResponse("/schedule", status_code=303)


@app.post("/epg/record")
def epg_record(
    channel_id: str = Form(...),
    title: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
):
    schedule_recording(channel_id, title, start_time, end_time)
    return RedirectResponse("/", status_code=303)


@app.post("/epg/cancel")
def epg_cancel(record_id: str = Form(...)):
    cancel_recording(record_id)
    return RedirectResponse("/", status_code=303)


@app.get("/cancel/{record_id}")
def cancel(record_id: str):
    cancel_recording(record_id)
    return RedirectResponse("/schedule", status_code=303)


@app.get("/delete/{record_id}")
def delete(record_id: str):
    url = (
        f"{VBOX_BASE_URL}/cgi-bin/HttpControl/HttpControlApp"
        f"?OPTION=1"
        f"&Method=DeleteRecord"
        f"&RecordID={urllib.parse.quote(record_id)}"
    )

    vbox_api(url)

    return RedirectResponse("/schedule", status_code=303)
