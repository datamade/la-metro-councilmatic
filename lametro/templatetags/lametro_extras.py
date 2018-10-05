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
from councilmatic_core.models import Person, EventDocument, Bill
from councilmatic_core.utils import ExactHighlighter

from lametro.models import LAMetroEvent
from lametro.utils import format_full_text, parse_subject

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
def prepare_title(full_text):
    formatted_text = format_full_text(full_text)

    if formatted_text:
        return parse_subject(formatted_text)


@register.filter
def full_text_doc_url(url):
    query = {'document_url': url, 'filename': 'agenda'}
    pic_query = {'file': PIC_BASE_URL + '?' + urllib.parse.urlencode(query)}

    return urllib.parse.urlencode(pic_query)


'''
This filter converts the post title into a prose-style format with nomination info,
(e.g., "Appointee of Los Angeles County City Selection Committee, Southeast Long Beach sector" into "Appointee of [Committee], nominated by the [Subcommittee]") 
Some posts do not require modification, e.g., "Caltrans District 7 Director, Appointee of Governor of California."
A full list of posts resides in the scraper: https://github.com/opencivicdata/scrapers-us-municipal/blob/master/lametro/people.py
'''
@register.filter
def appointment_label(label):
    full_label = label.replace("Appointee of", "Appointee of the")
    label_parts = full_label.split(', ')
    if len(label_parts) > 1 and 'Caltrans District 7 Director' not in full_label:
        if 'sector' in full_label:
            appointment_label = ', nominated by the '.join(label_parts).replace('sector', 'Subcommittee')
        else:
            appointment_label = ', nominated by the '.join(label_parts) + ' Subcommittee'
    else:
        appointment_label = full_label

    return appointment_label

@register.filter
def clean_membership_extras(extras):
    if extras.get('acting'):
        return 'Acting'
    else:
        return ''

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
    event = LAMetroEvent.objects.get(ocd_id=event_id)

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

@register.filter
def short_topic_name(text):
    if len(text) > 40:
        blurb = text[:40]
        blurb = blurb[:blurb.rfind(' ')] + ' ...'
        return blurb
    else:
        return text

@register.filter
def updates_made(event_id):
    ''' 
    When Metro finalizes an agenda, they add it to legistar, and we scrape this link, and add it to our DB. Sometimes, Metro might change the date or time of the event - after adding the agenda. When this occurs, a label indicates that event has been updated.
    This filter determines if an event had been updated after its related EventDocument (i.e., agenda) was last updated. 
    If the below equates as true, then we render a label with the text "Updated", next to the event, on the meetings page. 
    '''
    event = LAMetroEvent.objects.get(ocd_id=event_id)

    # Get the most recent updated agenda, if one of those agendas happens to be manually uploaded
    try: 
        document = EventDocument.objects.filter(event_id=event_id).latest('updated_at')
    except EventDocument.DoesNotExist:
        return False
    else:
        updated = document.updated_at < event.updated_at
        return updated 

@register.filter
def find_agenda_url(all_documents):
    '''
    This filter determines how to format the URL link, particularly, in the case of manually uploaded agenda.
    '''
    valid_urls = [x.url for x in all_documents if (x.note == 'Agenda' or x.note == 'Event Document - Manual upload URL')]
    pdf_url = [('static/' + x.url) for x in all_documents if x.note == 'Event Document - Manual upload PDF']
    valid_urls += pdf_url

    return valid_urls[0]

@register.simple_tag(takes_context=True)
def get_highlighted_attachment_text(context, ocd_id):
    bill = Bill.objects.get(ocd_id=ocd_id)
    attachment_text = ' '.join(d.full_text for d in bill.documents.all() if d.full_text)

    highlight = ExactHighlighter(context['query'])
    
    return highlight.highlight(attachment_text)
