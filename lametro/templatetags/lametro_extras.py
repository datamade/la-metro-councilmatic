from django import template
from django.template.defaultfilters import stringfilter
from django.utils.html import strip_entities, strip_tags
from django.utils import timezone

from haystack.query import SearchQuerySet
from datetime import date, timedelta, datetime
import re
import urllib

from councilmatic.settings_jurisdiction import *
from councilmatic.settings import PIC_BASE_URL
from councilmatic_core.models import Person, Event

register = template.Library()

@register.filter
def call_headshot_url(person_id):
    person = Person.objects.get(ocd_id=person_id)
    url = person.headshot_url
    return url

@register.filter
def call_link_html(person_id):
    person = Person.objects.get(ocd_id=person_id)
    url = person.link_html
    return url

@register.filter
def format_label(label):
    label_parts = label.split(', ')
    formatted_label = '<br />'.join(label_parts)

    return formatted_label

@register.filter
def format_district(label):
    label_parts = label.split(', ')
    if "Mayor of the City" in label:
        formatted_label = "City of Los Angeles"
    else:
        formatted_label = label_parts[-1]
    return formatted_label

# Filter for legislation detail view
@register.filter
def format_full_text(full_text):
    results = ''

    if full_text:
        txt_as_array = full_text.split("..")
        for item in txt_as_array:
            if 'SUBJECT:' in item:
                array_with_subject = item.split('\n\n')
                for item in array_with_subject:
                    if 'SUBJECT:' in item:
                        results = item.replace('\n', '')
    return results

# Filter for legislation detail view
@register.filter
def parse_subject(text):
    if text:
        before_keyword, keyword, after_keyword = text.partition('SUBJECT:')
        if after_keyword:
            if '[PROJECT OR SERVICE NAME]' not in after_keyword and '[DESCRIPTION]' not in after_keyword and '[CONTRACT NUMBER]' not in after_keyword:
                return after_keyword.strip()

    return None

@register.filter
def full_text_doc_url(url):
    print(url)
    query = {'document_url': url, 'filename': 'agenda'}
    print(query)
    pic_query = {'file': PIC_BASE_URL + '?' + urllib.parse.urlencode(query)}

    return urllib.parse.urlencode(pic_query)


@register.filter
def appointment_label(label):
    full_label = label.replace("Appointee of", "Appointee of the")
    label_parts = full_label.split(', ')
    if len(label_parts) > 1:
        if 'sector' in full_label:
            appointment_label = ', nominated by the '.join(label_parts).replace('sector', 'Subcommittee')
        else:
            appointment_label = ', nominated by the '.join(label_parts) + ' Subcommittee'
    else:
        appointment_label = full_label

    return appointment_label

@register.filter
def clean_role(role_list):
    if len(role_list) > 1:
        try:
            role_list.remove('Board Member')
        except:
            pass

    return role_list[0]

@register.filter
def clean_label(label_list):
    label_list = [ label for label in label_list if 'Chair' not in label ]
    label = label_list[0]

    return label

@register.filter
def format_string(label_list):
    label_list = label_list.replace('{', '').replace('}', '').replace('"', '')

    return label_list.split(',')

@register.filter
def get_minutes(event_id):
    event = Event.objects.get(ocd_id=event_id)

    doc = event.documents.filter(note__icontains='RBM Minutes').first()

    if doc:
        return doc.url
    else:
        date = event.start_time.date().strftime('%B %d, %Y')
        content = 'minutes of the regular board meeting held ' + date
        sqs = SearchQuerySet().filter(content=content).all()
        if sqs:
            for q in sqs:
                if (q.object.bill_type == 'Minutes' and 
                    q.object.slug and 
                    q.object.ocr_full_text):
                    if re.search(content, q.object.ocr_full_text, re.IGNORECASE):
                        return '/board-report/' + q.object.slug
        else:
            return None

@register.filter
def compare_time(event_date):
    if event_date < timezone.now():
        return True

@register.filter
@stringfilter
def revised_title(text_blob):
    session_dict = {
        '2014': '7/1/2014 to 6/30/2015',
        '2015': '7/1/2015 to 6/30/2016',
        '2016': '7/1/2016 to 6/30/2017',
        '2017': '7/1/2017 to 6/30/2018',
    }
    if text_blob in ['2014', '2015', '2016', '2017']:
        return session_dict[text_blob]
    else:
        return text_blob

@register.filter
def parse_agenda_item(text):
    if text:
        label, number = text.split(',')
        return number
    else: 
        return ''