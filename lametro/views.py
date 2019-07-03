import re
from itertools import groupby
from operator import attrgetter
import itertools
import urllib
import json
from datetime import date, timedelta, datetime, MINYEAR
from dateutil.relativedelta import relativedelta
from dateutil import parser
import requests
import sqlalchemy as sa
from collections import namedtuple
import json as simplejson
import os

from haystack.inputs import Raw, Exact
from haystack.query import SearchQuerySet
from requests.exceptions import HTTPError

from django.db import transaction, connection, connections
from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.shortcuts import render
from django.db.models.functions import Lower
from django.db.models import Max, Min, Prefetch
from django.utils import timezone
from django.utils.text import slugify
from django.views.generic import TemplateView, ListView
from django.http import HttpResponse, HttpResponseRedirect, HttpResponsePermanentRedirect, HttpResponseNotFound, JsonResponse
from django.shortcuts import render_to_response, redirect
from django.core import management
from django.views.generic import View
from django.views.decorators.csrf import csrf_exempt

from councilmatic_core.views import IndexView, BillDetailView, \
    CouncilMembersView, AboutView, CommitteeDetailView, CommitteesView, \
    PersonDetailView, EventDetailView, EventsView, CouncilmaticFacetedSearchView, \
    CouncilmaticSearchForm
from councilmatic_core.models import *

from lametro.models import LAMetroBill, LAMetroPost, LAMetroPerson, \
    LAMetroEvent, LAMetroOrganization, SubjectGuid
from lametro.forms import AgendaUrlForm, AgendaPdfForm

from councilmatic.settings_jurisdiction import MEMBER_BIOS
from councilmatic.settings import MERGER_BASE_URL, PIC_BASE_URL, SMART_LOGIC_KEY, \
    SMART_LOGIC_ENVIRONMENT


class LAMetroIndexView(IndexView):
    template_name = 'lametro/index.html'

    event_model = LAMetroEvent

    def extra_context(self):
        extra = {}
        extra['upcoming_board_meeting'] = self.event_model.upcoming_board_meeting()
        extra['current_meeting'] = self.event_model.current_meeting()
        extra['bilingual'] = bool([e for e in extra['current_meeting'] if e.bilingual])

        return extra

class LABillDetail(BillDetailView):
    model = LAMetroBill
    template_name = 'lametro/legislation.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['actions'] = self.get_object().actions.all().order_by('-order')
        context['attachments'] = self.get_object().attachments.all().order_by(Lower('note')).exclude(note="Board Report")

        context['board_report'] = self.get_object().attachments.get(note="Board Report")
        item = context['legislation']
        actions = Action.objects.filter(_bill_id=item.ocd_id)
        organization_lst = [action.organization for action in actions]
        context['sponsorships'] = set(organization_lst)

        # Create URL for packet download.
        packet_slug = item.ocd_id.replace('/', '-')
        try:
            r = requests.head(MERGER_BASE_URL + '/document/' + packet_slug)
            if r.status_code == 200:
                context['packet_url'] = MERGER_BASE_URL + '/document/' + packet_slug
        except:
            context['packet_url'] = None

        # Create list of related board reports, ordered by descending last_action_date.
        # Thanks https://stackoverflow.com/a/2179053 for how to handle null last_action_date
        if context['legislation'].related_bills.all():
            all_related_bills = context['legislation'].related_bills.all().values('related_bill_identifier')
            related_bills = LAMetroBill.objects.filter(identifier__in=all_related_bills)
            minimum_date = datetime(MINYEAR, 1, 1, tzinfo=app_timezone)
            context['related_bills'] = sorted(related_bills, key=lambda bill: bill.get_last_action_date() or minimum_date, reverse=True)


        return context


class LAMetroEventDetail(EventDetailView):
    model = LAMetroEvent
    template_name = 'lametro/event.html'

    def post(self, request, *args, **kwargs):
        self.object = self.get_object() # Assign object to detail view, so that get_context_data can find this variable: https://stackoverflow.com/questions/34460708/checkoutview-object-has-no-attribute-object
        event = self.get_object()
        event_slug = event.slug

        # Look for the button name and assign form values.
        if 'url_form' in request.POST:
            url_form = AgendaUrlForm(request.POST)
            pdf_form = AgendaPdfForm()
        elif 'pdf_form' in request.POST:
            pdf_form = AgendaPdfForm(request.POST, request.FILES)
            url_form = AgendaUrlForm()

        # Validate forms and redirect.
        if url_form.is_valid():
            agenda_url = url_form['agenda'].value()
            document_obj, created = EventDocument.objects.get_or_create(event=event,
                url=agenda_url, updated_at= timezone.now())
            document_obj.note = ('Event Document - Manual upload URL')
            document_obj.save()

            return HttpResponseRedirect('/event/%s' % event_slug)
        elif pdf_form.is_valid() and 'pdf_form' in request.POST:
            agenda_pdf = request.FILES['agenda']

            handle_uploaded_agenda(agenda=agenda_pdf, event=event)

            return HttpResponseRedirect('/event/%s' % event_slug)
        else:
            return self.render_to_response(self.get_context_data(url_form=url_form, pdf_form=pdf_form))

    def get_object(self):
        # Get the event with prefetched media_urls in proper order.
        event = LAMetroEvent.objects.with_media().get(slug=self.kwargs['slug'])

        return event

    def get_context_data(self, **kwargs):
        context = super(EventDetailView, self).get_context_data(**kwargs)
        event = context['event']

        # Metro admins should see a status report if Legistar is down.
        # GET the calendar page, which contains relevant URL for agendas.
        if self.request.user.is_authenticated():
            r = requests.get('https://metro.legistar.com/calendar.aspx')
            context['legistar_ok'] = r.ok

        # Create URL for packet download.
        packet_slug = event.ocd_id.replace('/', '-')
        try:
            r = requests.head(MERGER_BASE_URL + '/document/' + packet_slug)
            if r.status_code == 200:
                context['packet_url'] = MERGER_BASE_URL + '/document/' + packet_slug
            elif r.status_code == 404:
                context['packet_url'] = None
        except:
            context['packet_url'] = None

        # Logic for getting relevant board report information.
        with connection.cursor() as cursor:
            query = '''
                SELECT DISTINCT
                    b.identifier,
                    b.ocd_id,
                    b.slug,
                    b.ocr_full_text,
                    i.description,
                    d_bill.url,
                    i.order,
                    i.notes
                FROM councilmatic_core_billdocument AS d_bill
                INNER JOIN councilmatic_core_eventagendaitem as i
                ON i.bill_id=d_bill.bill_id
                INNER JOIN councilmatic_core_eventdocument as d_event
                ON i.event_id=d_event.event_id
                INNER JOIN councilmatic_core_bill AS b
                ON d_bill.bill_id=b.ocd_id
                WHERE d_event.event_id='{}'
                AND trim(lower(d_bill.note)) LIKE 'board report%'
                ORDER BY i.order
                '''.format(event.ocd_id)

            cursor.execute(query)

            # Get field names
            columns = [c[0] for c in cursor.description]
            columns.append('packet_url')
            columns.append('inferred_status')

            # Add field for packet_url
            cursor_copy = []
            packet_url = None

            for obj in cursor:
                packet_url = None
                # Add packet slug
                ocd_id = obj[1]
                packet_slug = ocd_id.replace('/', '-')

                r = requests.head(MERGER_BASE_URL + '/document/' + packet_slug)
                if r.status_code == 200:
                    packet_url = MERGER_BASE_URL + '/document/' + packet_slug

                obj = obj + (packet_url,)

                # The cursor object potentially includes public and private bills.
                # However, the LAMetroBillManager excludes private bills
                # from the LAMetroBill queryset.
                # Attempting to `get` a private bill from LAMetroBill.objects
                # raises a DoesNotExist error. As a precaution, we use filter()
                # rather than get().
                board_report = LAMetroBill.objects.filter(ocd_id=ocd_id)
                if board_report:
                    obj = obj + (board_report.first().inferred_status,)
                    cursor_copy.append(obj)

            # Create a named tuple
            board_report_tuple = namedtuple('BoardReportProperties', columns, rename=True)
            # Put results inside a list with assigned fields (from namedtuple)
            related_board_reports = [board_report_tuple(*r) for r in cursor_copy]

            # Find agenda link.
            if event.documents.all():
                for document in event.documents.all():
                    if "Agenda" in document.note:
                        context['agenda_url'] = document.url
                        context['document_timestamp'] = document.updated_at
                    elif "Manual upload URL" in document.note:
                        context['uploaded_agenda_url'] = document.url
                        context['document_timestamp'] = document.updated_at
                    elif "Manual upload PDF" in document.note:
                        context['uploaded_agenda_pdf'] = document.url
                        context['document_timestamp'] = document.updated_at
                        '''
                        LA Metro Councilmatic uses the adv_cache library to partially cache templates: in the event view, we cache the entire template, except the iframe. (N.B. With this library, the views do not cached, unless explicitly wrapped in a django cache decorator.
                        Nonetheless, several popular browsers (e.g., Chrome and Firefox) retrieve cached iframe images, regardless of the site's caching specifications.
                        We use the agenda's "updated_at" timestamp to bust the iframe cache: we save it inside context and then assign it as the "name" of the iframe, preventing the browser from retrieving a cached iframe, when the timestamp changes.
                        '''

            context['related_board_reports'] = related_board_reports
            context['base_url'] = PIC_BASE_URL # Give JS access to this variable

            # Render forms if not a POST request
            if 'url_form' not in context:
                context['url_form'] = AgendaUrlForm()
            if 'pdf_form' not in context:
                context['pdf_form'] = AgendaPdfForm()

        return context


def handle_uploaded_agenda(agenda, event):
    with open('lametro/static/pdf/agenda-%s.pdf' % event.slug, 'wb+') as destination:
        for chunk in agenda.chunks():
            destination.write(chunk)

    # Create the document in database
    document_obj, created = EventDocument.objects.get_or_create(event=event,
        url='pdf/agenda-%s.pdf' % event.slug, updated_at= timezone.now())
    document_obj.note = ('Event Document - Manual upload PDF')
    document_obj.save()

    # Collect static to render PDF on server
    management.call_command('collectstatic', '--noinput')


def delete_submission(request, event_slug):
    event = LAMetroEvent.objects.get(slug=event_slug)
    event_doc = EventDocument.objects.filter(event_id=event.ocd_id, note__icontains='Manual upload')

    for e in event_doc:
        # Remove stored PDF from Metro app.
        if 'Manual upload PDF' in e.note:
            try:
                os.remove('lametro/static/%s' % e.url )
            except OSError:
                pass
        e.delete()

    return HttpResponseRedirect('/event/%s' % event_slug)


class LAMetroEventsView(EventsView):
    template_name = 'lametro/events.html'

    def get_context_data(self, **kwargs):
        context = super(LAMetroEventsView, self).get_context_data(**kwargs)

        # Did the user set date boundaries?
        start_date_str = self.request.GET.get('from')
        end_date_str   = self.request.GET.get('to')
        day_grouper    = lambda x: (x.start_time.year, x.start_time.month, x.start_time.day)

        # If yes...
        if start_date_str and end_date_str:
            context['start_date'] = start_date_str
            context['end_date']   = end_date_str
            start_date_time       = parser.parse(start_date_str)
            end_date_time         = parser.parse(end_date_str)

            select_events = LAMetroEvent.objects\
                                        .with_media()\
                                        .filter(start_time__gt=start_date_time)\
                                        .filter(start_time__lt=end_date_time)\
                                        .order_by('start_time')\

            org_select_events = []

            for event_date, events in itertools.groupby(select_events, key=day_grouper):
                events = sorted(events, key=attrgetter('start_time'))
                org_select_events.append([date(*event_date), events])

            context['select_events'] = org_select_events

        # If all meetings
        elif self.request.GET.get('show'):
            all_events = LAMetroEvent.objects\
                                     .with_media()\
                                     .order_by('-start_time')\

            org_all_events = []

            for event_date, events in itertools.groupby(all_events, key=day_grouper):
                events = sorted(events, key=attrgetter('start_time'))
                org_all_events.append([date(*event_date), events])

            context['all_events'] = org_all_events
        # If no...
        else:
            # Upcoming events
            future_events = LAMetroEvent.objects\
                                        .with_media()\
                                        .filter(start_time__gt=timezone.now())\
                                        .order_by('start_time')\

            org_future_events = []

            for event_date, events in itertools.groupby(future_events, key=day_grouper):
                events = sorted(events, key=attrgetter('start_time'))
                org_future_events.append([date(*event_date), events])

            context['future_events'] = org_future_events

            # Past events
            past_events = LAMetroEvent.objects\
                                      .with_media()\
                                      .filter(start_time__lt=datetime.now(app_timezone))\
                                      .order_by('-start_time')\

            org_past_events = []

            for event_date, events in itertools.groupby(past_events, key=day_grouper):
                events = sorted(events, key=attrgetter('start_time'))
                org_past_events.append([date(*event_date), events])

            context['past_events'] = org_past_events

        context['user_subscribed'] = False
        if self.request.user.is_authenticated():
            user = self.request.user
            context['user'] = user

            if settings.USING_NOTIFICATIONS:
                if (len(user.eventssubscriptions.all()) > 0):
                    context['user_subscribed'] = True

        return context

class LABoardMembersView(CouncilMembersView):
    template_name = 'lametro/board_members.html'

    model = LAMetroPost

    def get_queryset(self):
        posts = LAMetroPost.objects.filter(_organization__ocd_id=settings.OCD_CITY_COUNCIL_ID)

        return posts

    def get_context_data(self, *args, **kwargs):
        context = super(LABoardMembersView, self).get_context_data(**kwargs)
        context['seo'] = self.get_seo_blob()

        context['map_geojson'] = None

        if settings.MAP_CONFIG:

            map_geojson = {
                            'type': 'FeatureCollection',
                            'features': []
                          }

            map_geojson_sectors = {
                                    'type': 'FeatureCollection',
                                    'features': []
                                  }

            map_geojson_city = {
                                 'type': 'FeatureCollection',
                                 'features': []
                               }

            for post in self.object_list:
                if post.shape:
                    council_member = "Vacant"
                    detail_link = ""
                    if post.current_members:
                        for membership in post.current_members:
                            council_member = membership.person.name
                            detail_link = membership.person.slug

                    feature = {
                        'type': 'Feature',
                        'geometry': json.loads(post.shape),
                        'properties': {
                            'district': post.label,
                            'council_member': council_member,
                            'detail_link': '/person/' + detail_link,
                            'select_id': 'polygon-{}'.format(slugify(post.label)),
                        },
                    }

                    if 'council_district' in post.division_ocd_id:
                        map_geojson['features'].append(feature)

                    if 'la_metro_sector' in post.division_ocd_id:
                        map_geojson_sectors['features'].append(feature)

                    if post.division_ocd_id == 'ocd-division/country:us/state:ca/place:los_angeles':
                        map_geojson_city['features'].append(feature)

            context['map_geojson'] = json.dumps(map_geojson)
            context['map_geojson_sectors'] = json.dumps(map_geojson_sectors)
            context['map_geojson_city'] = json.dumps(map_geojson_city)


        with connection.cursor() as cursor:
            today = timezone.now().date()

            sql = '''
            SELECT p.ocd_id, p.name, array_agg(pt.label) as label,
                m.extras,
                array_agg(m.role) as role,
                split_part(p.name, ' ', 2) AS last_name
            FROM councilmatic_core_membership as m
            INNER JOIN councilmatic_core_post as pt
            ON pt.ocd_id=m.post_id
            INNER JOIN councilmatic_core_person as p
            ON m.person_id=p.ocd_id
            WHERE m.organization_id='ocd-organization/42e23f04-de78-436a-bec5-ab240c1b977c'
            AND m.end_date >= '{0}'
            AND m.person_id <> 'ocd-person/912c8ddf-8d04-4f7f-847d-2daf84e096e2'
            GROUP BY p.ocd_id, p.name, m.extras
            ORDER BY last_name
            '''.format(today)

            cursor.execute(sql)

            columns = [c[0] for c in cursor.description]
            columns.append('index')
            cursor_copy = []

            # from operator import itemgetter
            for obj in cursor:
                if '1st Vice Chair' in obj[3]:
                    obj = obj + ("2",)
                elif '2nd Vice Chair' in obj[3]:
                    obj = obj + ("3",)
                elif 'Chair' in obj[3]:
                    obj = obj + ("1",)
                elif 'Nonvoting Board Member' in obj[3]:
                    obj = obj + ("5",)
                else:
                    obj = obj + ("4",)
                cursor_copy.append(obj)

            # Create tuple-like object...iterable and accessible by field names.
            membership_tuple = namedtuple('Membership', columns)
            membership_objects = [membership_tuple(*r) for r in cursor_copy]
            membership_objects = sorted(membership_objects, key=lambda x: x[5])
            context['membership_objects'] = membership_objects

            board = LAMetroOrganization.objects.get(ocd_id=settings.OCD_CITY_COUNCIL_ID)
            context['recent_activity'] = board.actions.order_by('-date', '-_bill__identifier', '-order')
            context['recent_events'] = board.recent_events

        return context


class LAMetroAboutView(AboutView):
    template_name = 'lametro/about.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context['timestamp'] = datetime.now(app_timezone).strftime('%m%d%Y%s')

        return context


class LACommitteesView(CommitteesView):
    template_name = 'lametro/committees.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        with connection.cursor() as cursor:

            sql = ('''
              SELECT DISTINCT on (o.ocd_id, m.person_id) o.*, m.person_id, m.role, p.name AS member_name
              FROM councilmatic_core_organization AS o
              JOIN councilmatic_core_membership AS m
              ON o.ocd_id=m.organization_id
              JOIN councilmatic_core_person as p
              ON p.ocd_id=m.person_id
              WHERE o.classification='committee'
              AND m.end_date::date > NOW()::date
              AND m.role != 'Chief Executive Officer'
              ORDER BY o.ocd_id, m.person_id, m.end_date;
                ''')

            cursor.execute(sql)

            columns           = [c[0] for c in cursor.description]
            committees_tuple  = namedtuple('Committee', columns, rename=True)
            data              = [committees_tuple(*r) for r in cursor]
            groups            = []

            for key, group in groupby(data, lambda x: x[1]):
                groups.append(list(group))

            committees_list = groups
            context["committees_list"] = committees_list

        return context

class LACommitteeDetailView(CommitteeDetailView):

    model = LAMetroOrganization
    template_name = 'lametro/committee.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        committee = context['committee']

        if getattr(settings, 'COMMITTEE_DESCRIPTIONS', None):
            description = settings.COMMITTEE_DESCRIPTIONS.get(committee.slug)
            context['committee_description'] = description

        with connection.cursor() as cursor:
            base_sql = '''
              SELECT
                m.role,
                p.name, p.slug, p.ocd_id,
                m.extras,
                array_agg(mm.label::VARCHAR)
                FILTER (WHERE mm.label is not Null) as label,
                split_part(p.name, ' ', 2) AS last_name
              FROM councilmatic_core_membership AS m
              LEFT JOIN (
                SELECT
                  person_id,
                  array_agg(DISTINCT pt.label) as label
                FROM councilmatic_core_membership AS m
                JOIN councilmatic_core_post AS pt
                  ON m.post_id=pt.ocd_id
                WHERE m.organization_id = %s
                GROUP BY person_id
              ) AS mm
                USING(person_id)
              JOIN councilmatic_core_person AS p
                ON m.person_id = p.ocd_id
              WHERE m.organization_id = %s
              AND m.end_date::date > NOW()::date
              AND m.role != 'Chief Executive Officer'
              GROUP BY
                m.role, p.name, p.slug, p.ocd_id, m.extras
              {order_by}
            '''
            committee_sql = base_sql.format(order_by="ORDER BY CASE " +
                                           "WHEN m.role='Chair' THEN 0 " +
                                           "WHEN m.role='Vice Chair' THEN 1 " +
                                           "WHEN m.role='Member' THEN 2" +
                                           "END")

            cursor.execute(committee_sql, [settings.OCD_CITY_COUNCIL_ID, committee.ocd_id])
            columns = [c[0] for c in cursor.description]
            results_tuple = namedtuple('Member', columns)
            objects_list = [results_tuple(*r) for r in cursor]

            context['membership_objects'] = objects_list

            ad_hoc_committee_sql = base_sql.format(order_by="ORDER BY CASE " +
                                           "WHEN m.role='Chair' THEN 0 " +
                                           "WHEN m.role='1st Vice Chair' THEN 1 " +
                                           "WHEN m.role='2nd Vice Chair' THEN 2 "
                                           "WHEN m.role='Member' THEN 3" +
                                           "END")

            cursor.execute(ad_hoc_committee_sql, [settings.OCD_CITY_COUNCIL_ID, committee.ocd_id])

            columns = [c[0] for c in cursor.description]
            results_tuple = namedtuple('Member', columns)
            objects_list = [results_tuple(*r) for r in cursor]

            context['ad_hoc_list'] = objects_list

            context['ceo'] = Person.objects.filter(memberships__role='Chief Executive Officer',                                   memberships___organization=committee.ocd_id).first()

        return context


class LAPersonDetailView(PersonDetailView):

    template_name = 'lametro/person.html'
    model = LAMetroPerson

    def dispatch(self, request, *args, **kwargs):
        slug = self.kwargs['slug']

        try:
            person = self.model.objects.get(slug=slug)

        except LAMetroPerson.DoesNotExist:
            person = None

        else:
            response = super().dispatch(request, *args, **kwargs)

        if not person:
            # Grab the first and last name from slug like "john-smith-af5a8ab39aad"
            short_slug = '-'.join(slug.split('-')[:-1])

            try:
                person = self.model.objects.get(slug__startswith=short_slug)
            except (LAMetroPerson.DoesNotExist, LAMetroPerson.MultipleObjectsReturned):
                # Return a 404 if more than one matching slug, or if there are no matching slugs
                response = HttpResponseNotFound()
            else:
                response = HttpResponsePermanentRedirect(reverse('person', args=[person.slug]))

        return response

    def get_context_data(self, **kwargs):
        post_model = LAMetroPost

        context = super().get_context_data(**kwargs)
        person = context['person']

        title = ''
        qualifying_post = '' # board membership criteria met by person in question
        m = person.latest_council_membership
        if person.current_council_seat:
            title = m.role
            if m.post:
                qualifying_post = m.post.label
                if m.extras.get('acting'):
                    qualifying_post = 'Acting' + ' ' + qualifying_post

        else:
            title = 'Former %s' % m.role
        context['title'] = title
        context['qualifying_post'] = qualifying_post

        if person.committee_sponsorships:
            context['sponsored_legislation'] = person.committee_sponsorships
        else:
            context['sponsored_legislation'] = []

        with connection.cursor() as cursor:

            sql = ('''
                SELECT o.name as organization, o.slug as org_slug, m.role
                FROM councilmatic_core_membership AS m
                JOIN councilmatic_core_organization AS o
                ON o.ocd_id = m.organization_id
                WHERE m.person_id = %s
                AND m.end_date::date > NOW()::date
                AND m.organization_id != %s
                ORDER BY
                    CASE
                        WHEN m.role='Chair' THEN 0
                        WHEN m.role='Vice Chair' THEN 1
                        WHEN m.role='Member' THEN 2
                    END
                ''')

            cursor.execute(sql, [person.ocd_id, settings.OCD_CITY_COUNCIL_ID])

            columns = [c[0] for c in cursor.description]

            results_tuple = namedtuple('Member', columns)
            memberships_list = [results_tuple(*r) for r in cursor]
            context['memberships_list'] = memberships_list

        if person.slug in MEMBER_BIOS:
            context['member_bio'] = MEMBER_BIOS[person.slug]

        return context


class LAMetroCouncilmaticSearchForm(CouncilmaticSearchForm):
    def __init__(self, *args, **kwargs):
        if kwargs.get('search_corpus'):
            self.search_corpus = kwargs.pop('search_corpus')

        self.result_type = kwargs.pop('result_type', None)

        super(LAMetroCouncilmaticSearchForm, self).__init__(*args, **kwargs)

    def search(self):
        sqs = super(LAMetroCouncilmaticSearchForm, self).search()

        if self.search_corpus == 'all':
            # Don't auto-escape my query! https://django-haystack.readthedocs.io/en/v2.4.1/searchqueryset_api.html#SearchQuerySet.filter
            sqs = sqs.filter_or(attachment_text=Raw(self.cleaned_data['q']))

        if self.result_type == 'keyword':
            sqs = sqs.exclude(topics__iexact=Exact(self.cleaned_data['q']))
        elif self.result_type == 'topic':
            sqs = sqs.filter(topics__iexact=Exact(self.cleaned_data['q']))

        return sqs


class LAMetroCouncilmaticFacetedSearchView(CouncilmaticFacetedSearchView):
    def __init__(self, *args, **kwargs):
        kwargs['form_class'] = LAMetroCouncilmaticSearchForm
        super(LAMetroCouncilmaticFacetedSearchView, self).__init__(*args, **kwargs)

    def build_form(self, form_kwargs={}):
        form = super(CouncilmaticFacetedSearchView, self).build_form(form_kwargs=form_kwargs)

        form_kwargs['selected_facets'] = self.request.GET.getlist("selected_facets")
        form_kwargs['search_corpus'] = 'all' if self.request.GET.get('search-all') else 'bills'
        form_kwargs['result_type'] = self.request.GET.get('result_type', 'all')

        sqs = SearchQuerySet().facet('bill_type', sort='index')\
                              .facet('sponsorships', sort='index')\
                              .facet('inferred_status')\
                              .facet('topics')\
                              .facet('legislative_session', sort='index')\
                              .highlight(**{'hl.fl': 'text,attachment_text'})

        data = None
        kwargs = {
            'load_all': self.load_all,
        }

        if form_kwargs:
            kwargs.update(form_kwargs)

        dataDict = {}
        if len(self.request.GET):
            data = self.request.GET
            dataDict = dict(data)

        if self.searchqueryset is not None:
            kwargs['searchqueryset'] = sqs

            if dataDict.get('sort_by'):
                for el in dataDict['sort_by']:
                    if el == 'date':
                        if dataDict.get('order_by') == ['asc']:
                            kwargs['searchqueryset'] = sqs.order_by('last_action_date')
                        else:
                            kwargs['searchqueryset'] = sqs.order_by('-last_action_date')
                    if el == 'title':
                        if dataDict.get('order_by') == ['desc']:
                            kwargs['searchqueryset'] = sqs.order_by('-sort_name')
                        else:
                            kwargs['searchqueryset'] = sqs.order_by('sort_name')
                    if el == 'relevance':
                        kwargs['searchqueryset'] = sqs

            elif dataDict.get('q'):
                kwargs['searchqueryset'] = sqs
            else:
                kwargs['searchqueryset'] = sqs.order_by('-last_action_date')

        return self.form_class(data, **kwargs)


class GoogleView(IndexView):
    template_name = 'lametro/google66b34bb6957ad66c.html'


class LAMetroArchiveSearch(TemplateView):
    template_name = 'lametro/archive_search.html'


def metro_login(request):
    logout(request)
    if request.method == 'POST':
        form = AuthenticationForm(data=request.POST)
        if form.is_valid():
            user = form.get_user()
            if user is not None:
                login(request, user)
                return HttpResponseRedirect('/events/')
    else:
        form = AuthenticationForm()
    return render(request, 'lametro/metro_login.html', {'form': form})


def metro_logout(request):
    logout(request)
    return HttpResponseRedirect('/')


@csrf_exempt
def refresh_guid_trigger(request, refresh_key):
    try:
        if refresh_key == settings.REFRESH_KEY:
            management.call_command('refresh_guid')
            return HttpResponse(200)
        else:
            print('You do not have the correct refresh_key to access this.')
    except AttributeError:
        print('You need a refresh_key in your local deployment settings files to access this.')
    return HttpResponse(403)


class SmartLogicAPI(ListView):
    api_key = SMART_LOGIC_KEY

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def render_to_response(self, context):
        '''
        Return response as JSON.
        '''
        try:
            return JsonResponse(context['object_list'])
        except json.JSONDecodeError:
            return JsonResponse({ 'status': 'false', 'message': 'No topic found' }, status=500)

    def get_queryset(self, *args, **kwargs):
        '''
        Hit the SmartLogic endpoint. Tokens expire in two weeks, so if we get
        an authentication-related status code, refresh the token and try again
        once before failing out.
        '''
        return self._generate_token()

    def _generate_token(self):
        '''
        Get a JSON Web Token from the SmartLogic API.
        '''
        url = 'https://cloud.smartlogic.com/token'
        params = {'grant_type': 'apikey', 'key': self.api_key}

        try:
            response = requests.post(url, data=params)
        except HTTPError as e:
            print('Could not authenticate with SmartLogic: {}'.format(e))
            return None

        try:
            return json.loads(response.content.decode('utf-8'))
        except json.JSONDecodeError:
            '''
            Occasionally we are returned a 200 response with the html of a SmartLogic page.
            We handle the json.JSONDecodeError that causes here.
            '''
            raise


def fetch_topic(request):

    '''
    Retrieves Subject title from given GUID. There maybe be more than one Subject mapped to a
    GUID in our SubjectGuid lookup table due to the way we have syncing set up. We only expect one
    canonical Subject from each GUID, so we handle the MultpleObjectsReturn exception.
    '''

    guid = request.GET['guid']

    response = {}
    response['guid'] = guid

    try:
        subject_guid = SubjectGuid.objects.get(guid=guid)
        subject = subject_guid.name
        response['subject_safe'] = urllib.parse.quote(subject)
        response['status_code'] = 200
    except MultipleObjectsReturned:
        subjects = [s.name for s in SubjectGuid.objects.filter(guid=guid)]
        subject = Subject.objects.get(subject__in=subjects)
        subject = subject.subject
        response['subject_safe'] = urllib.parse.quote(subject)
        response['status_code'] = 200
    except ObjectDoesNotExist:
        subject = ''
        subject_safe = ''
        response['status_code'] = 404

    response['subject'] = subject

    return JsonResponse(response)
