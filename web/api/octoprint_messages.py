from django.utils import timezone
import json
from typing import Dict

from lib import cache
from lib import channels
from lib.utils import set_as_str_if_present
from lib import mobile_notifications
from app.models import PrintEvent, Printer
from app.tasks import service_webhook
from lib.heater_trackers import process_heater_temps
from asgiref.sync import async_to_sync, sync_to_async
from channels.db import database_sync_to_async

STATUS_TTL_SECONDS = 240
SVC_WEBHOOK_PROGRESS_PCTS = [25, 50, 75]


async def process_octoprint_status(printer: Printer, status: Dict) -> None:
    octoprint_settings = status.get('octoprint_settings')
    if octoprint_settings:
        await sync_to_async(cache.printer_settings_set)(printer.id, settings_dict(octoprint_settings))

    # for backward compatibility
    if status.get('octoprint_data'):
        if 'octoprint_temperatures' in status:
            status['octoprint_data']['temperatures'] = status['octoprint_temperatures']

    if status.get('octoprint_data', {}).get('_ts'):   # data format for plugin 1.6.0 and higher
        await sync_to_async(cache.printer_status_set)(printer.id, json.dumps(status.get('octoprint_data', {})), ex=STATUS_TTL_SECONDS)
    else:
        octoprint_data: Dict = dict()
        set_as_str_if_present(octoprint_data, status.get('octoprint_data', {}), 'state')
        set_as_str_if_present(octoprint_data, status.get('octoprint_data', {}), 'progress')
        set_as_str_if_present(octoprint_data, status.get('octoprint_data', {}), 'file_metadata')
        set_as_str_if_present(octoprint_data, status.get('octoprint_data', {}), 'currentZ')
        set_as_str_if_present(octoprint_data, status.get('octoprint_data', {}), 'job')
        set_as_str_if_present(octoprint_data, status.get('octoprint_data', {}), 'temperatures')
        await sync_to_async(cache.printer_status_set)(printer.id, octoprint_data, ex=STATUS_TTL_SECONDS)

    if status.get('current_print_ts'):
        await database_sync_to_async(process_octoprint_status_with_ts)(status, printer)

    await sync_to_async(channels.send_status_to_web)(printer.id)

    temps = status.get('octoprint_data', {}).get('temperatures', None)
    if temps:
        await database_sync_to_async(process_heater_temps)(printer, temps)


def settings_dict(octoprint_settings):
    settings = dict(('webcam_' + k, str(v)) for k, v in octoprint_settings.get('webcam', {}).items())
    settings.update(dict(temp_profiles=json.dumps(octoprint_settings.get('temperature', {}).get('profiles', []))))
    settings.update(dict(printer_metadata=json.dumps(octoprint_settings.get('printer_metadata', {}))))
    return settings


def process_octoprint_status_with_ts(op_status, printer):
    op_event = op_status.get('octoprint_event', {})
    op_data = op_status.get('octoprint_data', {})
    print_ts = op_status.get('current_print_ts')
    current_filename = op_event.get('name') or op_data.get('job', {}).get('file', {}).get('name')
    if not current_filename:
        return
    printer.update_current_print(current_filename, print_ts)
    if not printer.current_print:
        return

    # Notification for mobile devices or for external service webhooks such as 3D Geeks
    # This has to happen before event saving, as `current_print` may change after event saving.
    mobile_notifications.send_if_needed(printer.current_print, op_event, op_data)
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

    if op_event.get('event_type') in mobile_notifications.PRINT_EVENTS:
        service_webhook.delay(printer.current_print.id, op_event.get('event_type'))

    print_time = op_data.get('progress', {}).get('printTime')
    print_time_left = op_data.get('progress', {}).get('printTimeLeft')
    pct = op_data.get('progress', {}).get('completion')
    last_progress = cache.print_progress_get(printer.current_print.id)
    next_progress_pct = next(iter(list(filter(lambda x: x > last_progress, SVC_WEBHOOK_PROGRESS_PCTS))), None)
    if pct and print_time and print_time_left and next_progress_pct and pct >= next_progress_pct:
        cache.print_progress_set(printer.current_print.id, next_progress_pct)
        service_webhook.delay(printer.current_print.id, 'PrintProgress', percent=pct, timeleft=int(print_time_left), currenttime=int(print_time))
