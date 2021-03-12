import re
import pytz
from datetime import datetime, timedelta
import requests
import lxml.html
from lxml.etree import tostring

from django.conf import settings
from django.utils import timezone

from councilmatic_core.models import Organization, Event

app_timezone = pytz.timezone(settings.TIME_ZONE)

def format_full_text(full_text):
    '''
    The search results and board report titles (on the BillDetail) should show the "SUBJECT:" header from the board report when present.
    The ocr_full_text contains this information. Some example snippets:

    # Subject header followed by two linebreaks.
    ..Subject\nSUBJECT:\tFOOD SERVICE OPERATOR\n\n..Action\nACTION:\tAWARD SERVICES CONTRACT\n\n..

    # Subject header followed by a return carriage and linebreak.
    ..Subject/Action\r\nSUBJECT: MONTHLY REPORT ON CRENSHAW/LAX SAFETY\r\nACTION: RECEIVE AND FILE\r\n

    # Subject header with a linebreak in the middle and without an ACTION header.
    ..Subject\nSUBJECT:    REVISED MOTION BY DIRECTORS HAHN, SOLIS, \nGARCIA, AND DUPONT-WALKER\n..Title\n
    '''
    results = ''

    if full_text:
        clean_full_text = full_text.replace('\n\n', 'NEWLINE').replace('\r\n', 'NEWLINE').replace('\n..', 'NEWLINE').replace('\n', ' ')
        match = re.search('(SUBJECT:)(.*?)(NEWLINE|ACTION:)', clean_full_text)
        if match:
            results = match.group(2)

    return results

def parse_subject(text):
    if ('[PROJECT OR SERVICE NAME]' not in text) and ('[DESCRIPTION]' not in text) and ('[CONTRACT NUMBER]' not in text):
        return text.strip()
