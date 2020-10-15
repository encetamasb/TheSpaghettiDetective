from django.utils import timezone
import json

from lib import redis
from lib import channels
from lib.utils import set_as_str_if_present
from app.models import PrintEvent
from app.tasks import service_webhook

STATUS_TTL_SECONDS = 240
SVC_WEBHOOK_EVENTS = ['PrintResumed', 'PrintPaused',
                      'PrintFailed', 'PrintDone', 'PrintCancelled', 'PrintStarted']
SVC_WEBHOOK_PROGRESS_PCTS = [25, 50, 75]


def process_octoprint_status(printer, status):
    octoprint_settings = status.get('octoprint_settings')
    if octoprint_settings:
        redis.printer_settings_set(printer.id, settings_dict(octoprint_settings))

    octoprint_data = dict()
    set_as_str_if_present(octoprint_data, status.get('octoprint_data', {}), 'state')
    set_as_str_if_present(octoprint_data, status.get('octoprint_data', {}), 'progress')
    set_as_str_if_present(octoprint_data, status,
                          'octoprint_temperatures', 'temperatures')
    import logging
    logging.error(octoprint_data)
    redis.printer_status_set(printer.id, octoprint_data, ex=STATUS_TTL_SECONDS)

    if status.get('current_print_ts'):
        process_octoprint_status_with_ts(status, printer)

    channels.send_status_to_web(printer.id)


"""

{
    "octoprint_data":
        {
            "state": {
                "text": "Starting",
                "flags": {
                    "operational": true,
                    "printing": true,
                    "cancelling": false,
                    "pausing": false,
                    "resuming": false,
                    "finishing": false,
                    "closedOrError": false,
                    "error": false,
                    "paused": false,
                    "ready": false,
                    "sdReady": true
                }
            },
            "job": {
                "file": {
                    "name": "a.gcode",
                    "path": "a.gcode",
                    "display": "a.gcode",
                    "origin": "local",
                    "size": 154006,
                    "date": 1600166555
                },
                "estimatedPrintTime": 412.9631944864423,
                "averagePrintTime": 63.40596257360376,
                "lastPrintTime": 64.48117619601544,
                "filament": {
                    "tool0": {"length": 1180.2499699999985, "volume": 2.83883386128792}
                },
                "user": "a"
            },
            "currentZ": null,
            "progress": {
                "completion": 0.0, "filepos": 0, "printTime": 0, "printTimeLeft": null, "printTimeLeftOrigin": null
            },
            "offsets": {}
        },
        "octoprint_temperatures": {
            "tool0": {"actual": 34.4, "target": 0.0, "offset": 0},
            "bed": {"actual": 40.0, "target": 40.0, "offset": 0},
            "chamber": {"actual": null, "target": null, "offset": 0}
        },
        "current_print_ts": -1,
        "octoprint_event": {
            "event_type": "PrinterStateChanged",
            "data": {"state_id": "STARTING", "state_string": "Starting"}
         }
    }
web_1    | api.consumers INFO     {"octoprint_data": {"state": {"text": "Starting", "flags": {"operational": true, "printing": true, "cancelling": false, "pausing": false, "resuming": false, "finishing": false, "closedOrError": false, "error": false, "paused": false, "ready": false, "sdReady": true}}, "job": {"file": {"name": "a.gcode", "path": "a.gcode", "display": "a.gcode", "origin": "local", "size": 154006, "date": 1600166555}, "estimatedPrintTime": 412.9631944864423, "averagePrintTime": 63.40596257360376, "lastPrintTime": 64.48117619601544, "filament": {"tool0": {"length": 1180.2499699999985, "volume": 2.83883386128792}}, "user": "a"}, "currentZ": null, "progress": {"completion": 0.0, "filepos": 0, "printTime": 0, "printTimeLeft": null, "printTimeLeftOrigin": null}, "offsets": {}}, "octoprint_temperatures": {"tool0": {"actual": 34.4, "target": 0.0, "offset": 0}, "bed": {"actual": 40.0, "target": 40.0, "offset": 0}, "chamber": {"actual": null, "target": null, "offset": 0}}, "current_print_ts": 1601643000, "octoprint_event": {"event_type": "PrintStarted", "data": {"name": "a.gcode", "path": "a.gcode", "origin": "local", "size": 154006, "owner": "a", "user": "a"}}}
web_1    | api.consumers INFO     {"octoprint_data": {"state": {"text": "Printing", "flags": {"operational": true, "printing": true, "cancelling": false, "pausing": false, "resuming": false, "finishing": false, "closedOrError": false, "error": false, "paused": false, "ready": false, "sdReady": true}}, "job": {"file": {"name": "a.gcode", "path": "a.gcode", "display": "a.gcode", "origin": "local", "size": 154006, "date": 1600166555}, "estimatedPrintTime": 412.9631944864423, "averagePrintTime": 63.40596257360376, "lastPrintTime": 64.48117619601544, "filament": {"tool0": {"length": 1180.2499699999985, "volume": 2.83883386128792}}, "user": "a"}, "currentZ": null, "progress": {"completion": 0.19479760528810566, "filepos": 300, "printTime": 0, "printTimeLeft": 63, "printTimeLeftOrigin": "average"}, "offsets": {}}, "octoprint_temperatures": {"tool0": {"actual": 34.4, "target": 0.0, "offset": 0}, "bed": {"actual": 40.0, "target": 40.0, "offset": 0}, "chamber": {"actual": null, "target": null, "offset": 0}}, "current_print_ts": 1601643000, "octoprint_event": {"event_type": "PrinterStateChanged", "data": {"state_id": "PRINTING", "state_string": "Printing"}}}
"""


def process_moonraker_status(printer, status):
    pstate = status['printer_state']
    if pstate is None:
        redis.printer_status_delete(printer.id)
        channels.send_status_to_web(printer.id)
        return

    kstate = pstate['klippy_state']
    if kstate is None:
        redis.printer_status_delete(printer.id)
        channels.send_status_to_web(printer.id)
        return

    flag = pstate['flag']
    kflag = kstate['print_stats__state']

    # isIdle in frontend is tied to state_text == Operational # FIXME
    state_text = {
        'standby': 'Operational',
        'printing': 'Printing',
        'paused': 'Paused',
        'error': (
            kstate['message']
            if 'error' in kstate['message']
            else 'error: ' + kstate['message']
        ),
        'complete': 'Operational',
    }.get(kflag, '')

    print_time = kstate.get('print_time', 0) or 0

    ev = {'event_type': pstate['print_event']} if pstate['print_event'] else {}

    octoprint_status = {
        'current_print_ts': pstate['current_print_ts'],
        'octoprint_event': ev,
        'octoprint_data': {
            'state': {
                '_from': 'moonraker',
                'text': state_text,
                'flags': {
                    'operational': flag == 'idle',
                    'printing': flag in 'printing',
                    # 'cancelling': False,
                    'pausing': flag == 'pausing',
                    'resuming': flag == 'resuming',
                    # 'finishing': False
                    'closedOrError': False,  # ==  isDisconnected in frontend
                    'error': kflag == 'error',
                    'paused': flag == 'paused',
                    # 'ready': False,
                    # 'sdReady': True
                },
            },
            'progress': {
                'completion': kstate.get('progress', 0.0) * 100.0 or 0.0,
                'filepos': kstate.get('file_position', 0) or 0,
                'printTime': print_time,
                'printTimeLeft': max(0, kstate.get('estimated_print_time', 0) - print_time),
                'printTimeLeftOrigin': None,  # "average"
            },
        },
        'octoprint_temperatures': {
            'tool0': {'actual': 90.0, 'target': 90.0, 'offset': 0},
            'bed': {'actual': 21.3, 'target': 0.0, 'offset': 0},
            'chamber': {'actual': None, 'target': None, 'offset': 0},
        }
    }

    return process_octoprint_status(printer, octoprint_status)


def settings_dict(octoprint_settings):
    settings = dict(('webcam_' + k, str(v))
                    for k, v in octoprint_settings.get('webcam', {}).items())
    settings.update(dict(temp_profiles=json.dumps(
        octoprint_settings.get('temperature', {}).get('profiles', []))))
    settings.update(dict(printer_metadata=json.dumps(
        octoprint_settings.get('printer_metadata', {}))))
    return settings


def process_octoprint_status_with_ts(op_status, printer):
    op_event = op_status.get('octoprint_event', {})
    op_data = op_status.get('octoprint_data', {})
    print_ts = op_status.get('current_print_ts')
    current_filename = op_event.get('name') or op_data.get(
        'job', {}).get('file', {}).get('name')
    if not current_filename:
        return
    printer.update_current_print(current_filename, print_ts)
    if not printer.current_print:
        return

    # Events for external service webhooks such as 3D Geeks
    # This has to happen before event saving, as `current_print` may change after event saving.
    call_service_webhook_if_needed(printer, op_event, op_data)

    if op_event.get('event_type') in ('PrintCancelled', 'PrintFailed'):
        printer.current_print.cancelled_at = timezone.now()
        printer.current_print.save()
    if op_event.get('event_type') in ('PrintFailed', 'PrintDone'):
        printer.unset_current_print()
    if op_event.get('event_type') == 'PrintPaused':
        printer.current_print.paused_at = timezone.now()
        printer.current_print.save()
        PrintEvent.create(printer.current_print, PrintEvent.PAUSED)
    if op_event.get('event_type') == 'PrintResumed':
        printer.current_print.paused_at = None
        printer.current_print.save()
        PrintEvent.create(printer.current_print, PrintEvent.RESUMED)


def call_service_webhook_if_needed(printer, op_event, op_data):
    if not printer.service_token:
        return

    if op_event.get('event_type') in SVC_WEBHOOK_EVENTS:
        service_webhook.delay(printer.current_print.id, op_event.get('event_type'))

    print_time = op_data.get('progress', {}).get('printTime')
    print_time_left = op_data.get('progress', {}).get('printTimeLeft')
    pct = op_data.get('progress', {}).get('completion')
    last_progress = redis.print_progress_get(printer.current_print.id)
    next_progress_pct = next(
        iter(list(filter(lambda x: x > last_progress, SVC_WEBHOOK_PROGRESS_PCTS))), None)
    if pct and print_time and print_time_left and next_progress_pct and pct >= next_progress_pct:
        redis.print_progress_set(printer.current_print.id, next_progress_pct)
        service_webhook.delay(printer.current_print.id, 'PrintProgress', percent=pct, timeleft=int(
            print_time_left), currenttime=int(print_time))
