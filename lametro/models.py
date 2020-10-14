from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import pytz
import re

import requests
from django.conf import settings
from django.db import models, connection
from django.db.models.expressions import RawSQL
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from django.utils.functional import cached_property
from django.contrib.auth.models import User
from django.db.models import Max, Min, Prefetch, Case, When, Value, Q, F
from django.db.models.functions import Now, Cast
import django.contrib.postgres.fields as postgres_fields
from django.contrib.staticfiles.templatetags.staticfiles import static
from opencivicdata.legislative.models import EventMedia, EventDocument, \
    EventDocumentLink, EventAgendaItem, EventRelatedEntity, RelatedBill, \
    BillVersion
from proxy_overrides.related import ProxyForeignKey

import councilmatic_core.models
from councilmatic_core.models import Bill, Event, Post, Person, Organization, \
    EventManager, Membership

from lametro.utils import format_full_text, parse_subject


app_timezone = pytz.timezone(settings.TIME_ZONE)

class SourcesMixin(object):

    @property
    def web_source(self):
        return self.sources.get(note='web')

    @property
    def api_source(self):
        return self.sources.get(note='api')


class LAMetroBillManager(models.Manager):
    def get_queryset(self):
        '''
        The Councilmatic database contains both "private" and "public" bills.
        This issue thread explains why:
        https://github.com/datamade/la-metro-councilmatic/issues/345#issuecomment-455683240

        We do not display "private" bills in Councilmatic.
        Metro staff devised three checks for knowing when to hide or show a report:

        (1) Is the bill private (i.e., `restrict_view` is True)? Then, do not show it.
        N.b., the scrapers contain logic for populating the restrict_view field:
        https://github.com/opencivicdata/scrapers-us-municipal/blob/master/lametro/bills.py

        (2) Does the Bill have a classification of "Board Box"? Then, show it.

        (3) Is the Bill on a published agenda, i.e., an event with the
        status of "passed" or "cancelled"? Then, show it.

        NOTE! A single bill can appear on multiple event agendas.
        We thus call 'distinct' on the below query, otherwise
        the queryset would contain duplicate bills.

        WARNING! Be sure to use LAMetroBill, rather than the base Bill class,
        when getting bill querysets. Otherwise restricted view bills
        may slip through the crevices of Councilmatic display logic.
        '''
        qs = super().get_queryset()

        qs = qs.exclude(
            extras__restrict_view=True
        ).annotate(board_box=Case(
            When(extras__local_classification='Board Box', then=True),
            When(classification__contains=['Board Box'], then=True),
            default=False,
            output_field=models.BooleanField()
        )).filter(Q(eventrelatedentity__agenda_item__event__status='passed') | \
                  Q(eventrelatedentity__agenda_item__event__status='cancelled') | \
                  Q(board_box=True)
        ).distinct()

        return qs


class LAMetroBill(Bill, SourcesMixin):
    objects = LAMetroBillManager()

    class Meta:
        proxy = True

    # LA METRO CUSTOMIZATION
    @property
    def friendly_name(self):
        full_text = self.extras.get('plain_text')

        results = None

        if full_text:
            results = format_full_text(full_text)

        if results:
            title = parse_subject(results)
        else:
            title = self.bill_type

        return '{0} - {1}'.format(self.identifier, title.upper())

    # LA METRO CUSTOMIZATION
    @property
    def inferred_status(self):
        # Get most recent action.
        action = self.actions.last()

        # Get description of that action.
        if action:
            description = action.description
        else:
            description = ''

        return self._status(description)

    def _status(self, description):
        if description:
            if description.upper() in ['APPROVED', 'APPROVED AS AMENDED', 'APPROVED THE CONSENT CALENDAR']:
                return 'Approved'
            elif description.upper() in ['ADOPTED', 'ADOPTED AS AMENDED']:
                return 'Adopted'
            elif description.upper() in ['RECOMMENDED FOR APPROVAL', 'RECOMMENDED FOR APPROVAL AS AMENDED', 'REFERRED', 'FORWARDED DUE TO ABSENCES AND CONFLICTS', 'FORWARDED WITHOUT RECOMMENDATION', 'NO ACTION TAKEN', 'NOT DISCUSSED']:
                return 'Active'
            elif description.upper() in ['RECEIVED', 'RECEIVED AND FILED']:
                return 'Received'
            elif description.upper() == 'FAILED':
                return 'Failed'
            elif description.upper() == 'WITHDRAWN':
              return 'Withdrawn'
        else:
            return None

    # LA METRO CUSTOMIZATION
    @property
    def attachments(self):
        return self.documents.all()

    @property
    def controlling_body(self):
        return self.from_organization

    @property
    def topics(self):
        return sorted(self.subject)

    @property
    def rich_topics(self):
        return LAMetroSubject.objects.filter(name__in=self.subject).order_by('name')

    @property
    def board_report(self):

        try:
            br = self.versions.get(note="Board Report")
            br.url = br.links.get().url
        except BillVersion.DoesNotExist:
            br = None

        return br

    @property
    def actions_and_agendas(self):
        '''
        Return list of dictionaries, each representing an action or an agenda on
        which the given bill appears, in the format:

        {
            'date': <datetime>,
            'description': <str>,
            'event': <LAMetroEvent>,
            'organization': <Organization>
        }
        '''
        actions = self.actions.all()
        events = LAMetroEvent.objects.filter(agenda__related_entities__bill=self)

        data = []

        for action in actions:
            event = LAMetroEvent.objects.get(\
                participants__entity_type='organization',\
                participants__name=action.organization,
                start_time__date=action.date)

            
            action_dict = {
                'date': action.date_dt,
                'description': action.description,
                'event': event,
                'organization': action.organization
            }
            
            data.append(action_dict)

        for event in events:
            # Use a description of "SCHEDULED"
            org = event.participants.first()
            event_dict = {
                'date': event.start_time.date(),
                'description': "SCHEDULED",
                'event': event,
                'organization': org
            }

            data.append(event_dict)

        sorted_data = sorted(data, key=lambda x: x['date'])

        return sorted_data


class RelatedBill(RelatedBill):

    class Meta:
        proxy = True

    bill = ProxyForeignKey(LAMetroBill,
                           related_name='related_bills',
                           on_delete=models.CASCADE)

    related_bill = ProxyForeignKey(LAMetroBill,
                                   related_name='related_bills_reverse',
                                   null=True,
                                   on_delete=models.SET_NULL)


class LAMetroPost(Post):

    class Meta:
        proxy = True

    @property
    def acting_label(self):
        if self.extras.get('acting'):
            return 'Acting ' + self.label
        else:
            return self.label


class LAMetroPerson(Person, SourcesMixin):

    class Meta:
        proxy = True

    @property
    def latest_council_membership(self):
        filter_kwarg = {'organization__name': settings.OCD_CITY_COUNCIL_NAME,}
        city_council_memberships = self.memberships.filter(**filter_kwarg)

        # Select posts denoting membership, i.e., exclude leadership
        # posts, like 1st Chair.
        # See: https://github.com/opencivicdata/python-opencivicdata/issues/129
        #
        # N.b., the CEO is not *technically* a member of the board, but their
        # role is on the membership side of the membership/leadership dichotomy.
        # See: https://github.com/datamade/la-metro-councilmatic/issues/351
        primary_memberships = city_council_memberships.filter(Q(role='Chief Executive Officer') |
                                                              Q(role='Board Member') |
                                                              Q(role='Nonvoting Board Member'))

        if primary_memberships.exists():
            return primary_memberships.order_by('-end_date').first()
        return None

    @property
    def current_district(self):
        m = self.latest_council_membership
        if m and m.post:
            return m.post.label
        return ''

    @property
    def board_office(self):
        office_roles = ('Chair', '1st Chair', 'Vice Chair', '1st Vice Chair', '2nd Chair', '2nd Vice Chair')

        try:
            office_membership = self.memberships\
                                    .filter(organization__name=settings.OCD_CITY_COUNCIL_NAME,
                                            role__in=office_roles)\
                                    .get(end_date_dt__gt=Now())
        except Membership.DoesNotExist:
            office_membership = None

        return office_membership

    @cached_property
    def committee_sponsorships(self):
        '''
        This property returns a list of five bills, which have recent actions
        from the organizations that the person has memberships in.

        Organizations do not include the Board of Directors.
        '''
        qs = LAMetroBill.objects\
            .defer('extras')\
            .filter(actions__organization__classification='committee',
                    actions__organization__memberships__in=self.current_memberships)\
            .order_by('-actions__date')\
            .distinct()[:5]

        return qs

    @classmethod
    def ceo(cls):
        try:
            ceo = Membership.objects\
                .get(post__role='Chief Executive Officer',
                     end_date_dt__gt=Now())\
                .person
        except Membership.DoesNotExist:
            ceo = None

        return ceo

    @property
    def headshot_url(self):
        if self.slug in settings.MANUAL_HEADSHOTS:
            image_url = 'images/' + settings.MANUAL_HEADSHOTS[self.slug]['image']
        elif self.headshot:
            image_url = self.headshot.url
        else:
            image_url = 'images/headshot_placeholder.png'

        return static(image_url)


class LAMetroEventManager(EventManager):
    def get_queryset(self):
        '''
        If SHOW_TEST_EVENTS is False, omit them from the initial queryset.

        NOTE: Be sure to use LAMetroEvent, rather than the base Event class,
        when getting event querysets. If a test event slips through, it is
        likely because we used the default Event to get the queryset.
        '''
        if settings.SHOW_TEST_EVENTS:
            return super().get_queryset()

        return super().get_queryset().exclude(location__name='TEST')

    def with_media(self):
        '''
        This function proves useful in the EventDetailView
        and EventsView (which returns all Events – often, filtered and sorted).

        We prefetch EventMedia (i.e., 'media_urls') in these views:
        this makes for a more efficient page load.
        We also order the 'media_urls' by label, ensuring that links to SAP audio
        come after links to English audio. 'mediaqueryset' facilitates
        the ordering of prefetched 'media_urls'.
        '''
        mediaqueryset = EventMedia.objects.annotate(
            olabel=Case(
                When(note__endswith='(SAP)', then=Value(0)),
                output_field=models.CharField(),
            )
        ).order_by('-olabel')

        return self.prefetch_related(Prefetch('media', queryset=mediaqueryset))\
                   .prefetch_related('media__links')


class LiveMediaMixin(object):
    BASE_MEDIA_URL = 'http://metro.granicus.com/mediaplayer.php?'
    GENERIC_ENGLISH_MEDIA_URL = BASE_MEDIA_URL + 'camera_id=3'
    GENERIC_SPANISH_MEDIA_URL = BASE_MEDIA_URL + 'camera_id=2'

    @property
    def bilingual(self):
        '''
        Upstream, when an English-language event can be paired with a Spanish-
        language event, the GUID of the Spanish-language event is added to the
        extras dictionary, e.g., return True if the sap_guid is present, else
        return False.
        '''
        return bool(self.extras.get('sap_guid'))


    def _valid(self, media_url):
        response = requests.get(media_url)

        if response.ok and 'The event you selected is not currently in progress' not in response.text:
            return True
        else:
            return False


    @property
    def english_live_media_url(self):
        guid = self.extras['guid']
        english_url = self.BASE_MEDIA_URL + 'event_id={guid}'.format(guid=guid)

        if self._valid(english_url):
            return english_url
        else:
            return self.GENERIC_ENGLISH_MEDIA_URL


    @property
    def spanish_live_media_url(self):
        '''
        If there is not an associated Spanish event, there will not be
        Spanish audio for the event, e.g., return None.
        '''
        if self.bilingual:
            guid = self.extras['sap_guid']
            spanish_url = self.BASE_MEDIA_URL + 'event_id={guid}'.format(guid=guid)

            if self._valid(spanish_url):
                return spanish_url
            else:
                return self.GENERIC_SPANISH_MEDIA_URL

        else:
            return None


class LAMetroEvent(Event, LiveMediaMixin, SourcesMixin):
    GENERIC_ECOMMENT_URL = 'https://metro.granicusideas.com/meetings?scope=past'

    UPCOMING_ECOMMENT_MESSAGE = (
        'Online public comment will be available on this page once the meeting '
        'begins.'
    )

    PASSED_ECOMMENT_MESSAGE = 'Online public comment for this meeting has closed.'

    objects = LAMetroEventManager()

    class Meta:
        proxy = True

    @classmethod
    def upcoming_board_meetings(cls):
        '''
        In rare instances, there are two board meetings in a given month, e.g.,
        one regular meeting and one special meeting. Display both on the
        homepage by returning all upcoming board meetings scheduled for the
        month of the next upcoming meeting.
        '''
        board_meetings = cls.objects.filter(name__icontains='Board Meeting',
                                            start_time__gt=timezone.now())\
                                    .order_by('start_time')

        next_meeting = board_meetings.first()

        return board_meetings.filter(start_time__month=next_meeting.start_time.month)\
                             .order_by('start_time')


    @staticmethod
    def _time_ago(**kwargs):
        '''
        Convenience method for returning localized, negative timedeltas.
        '''
        return timezone.now() - relativedelta(**kwargs)


    @staticmethod
    def _time_from_now(**kwargs):
        '''
        Convenience method for returning localized, positive timedeltas.
        '''
        return timezone.now() + relativedelta(**kwargs)


    @classmethod
    def _potentially_current_meetings(cls):
        '''
        Return meetings that could be "current" – that is, meetings that are
        scheduled to start in the last six hours, or in the next five minutes.

        Fun fact: The longest Metro meeting on record is 5.38 hours long (see
        issue #251). Hence, we check for meetings scheduled to begin up to six
        hours ago.

        Used to determine whether to check Granicus for streaming meetings.
        '''
        six_hours_ago = cls._time_ago(hours=6)
        five_minutes_from_now = cls._time_from_now(minutes=5)

        return cls.objects.filter(start_time__gte=six_hours_ago,
                                  start_time__lte=five_minutes_from_now)\
                           .exclude(status='cancelled')


    @classmethod
    def _streaming_meeting(cls):
        '''
        Granicus provides a running events endpoint that returns an array of
        GUIDs for streaming meetings. Metro events occur one at a time, but two
        GUIDs appear when an event is live: one for the English audio, and one
        for the Spanish audio.

        Hit the endpoint, and return the corresponding meeting, or an empty
        queryset.
        '''
        running_events = requests.get('http://metro.granicus.com/running_events.php')

        for guid in running_events.json():
            # We get back two GUIDs, but we won't know which is the English
            # audio GUID stored in the 'guid' field of the extras dict. Thus,
            # we iterate.
            #
            # Note that our stored GUIDs are all uppercase, because they come
            # that way from the Legistar API. The running events endpoint
            # returns all-lowercase GUIDs, so we need to uppercase them for
            # comparison.
            meeting = cls.objects.filter(extras__guid=guid.upper())

            if meeting:
                return meeting

        return cls.objects.none()


    @classmethod
    def current_meeting(cls):
        '''
        If there is a meeting scheduled to begin in the last six hours or in
        the next five minutes, hit the running events endpoint.

        If there is a running event, return the corresponding meeting.

        If there are no running events, return meetings scheduled to begin in
        the last 20 minutes (to account for late starts) or in the next five
        minutes (to show meetings as current, five minutes ahead of time).

        Otherwise, return an empty queryset.
        '''
        scheduled_meetings = cls._potentially_current_meetings()

        if scheduled_meetings:
            streaming_meeting = cls._streaming_meeting()

            if streaming_meeting:
                current_meetings = streaming_meeting

            else:
                # Sometimes, streams start later than a meeting's start time.
                # Check for meetings scheduled to begin in the last 20 minutes
                # so they are returned as current in the event that the stream
                # does not start on time.
                #
                # Note that 'scheduled_meetings' already contains meetings
                # scheduled to start in the last six hours or in the next five
                # minutes, so we just need to add the 20-minute lower bound to
                # return meetings scheduled in the last 20 minutes or in the
                # next five minutes.
                twenty_minutes_ago = cls._time_ago(minutes=20)

                # '.annotate' adds a boolean field, 'is_board_meeting'. We want
                # to show board meetings first, so order in reverse, since False
                # (0) comes before True (1).
                current_meetings = scheduled_meetings.filter(start_time__gte=twenty_minutes_ago)\
                                                     .annotate(is_board_meeting=RawSQL("opencivicdata_event.name like %s", ('%Board Meeting%',)))\
                                                     .order_by('-is_board_meeting')

        else:
            current_meetings = cls.objects.none()

        return current_meetings


    @classmethod
    def upcoming_committee_meetings(cls):
        '''
        Show a minimum of five meetings, up to and including the next board
        meeting.

        NOTE: This property name is a misnomer, inherited from django-councilmatic.
        '''
        date_of_next_board_meeting = cls.upcoming_board_meetings().last().start_time

        # Sometimes there are meetings scheduled for the same time as the
        # board meeting. Show the board meeting last.
        meetings = cls.objects.filter(start_time__gt=timezone.now())\
                              .annotate(is_board_meeting=RawSQL("opencivicdata_event.name like %s", ('%Board Meeting%',)))\
                              .order_by('start_time', 'is_board_meeting')

        if meetings.filter(start_time__lte=date_of_next_board_meeting).count() >= 5:
            return meetings.filter(start_time__lte=date_of_next_board_meeting)

        else:
            return meetings[:5]


    @property
    def is_upcoming(self):
        '''
        An event is upcoming starting at 5 p.m. local time the evening before
        its start time and ending once the meeting begins.
        '''
        local_now = app_timezone.localize(datetime.now())

        day_before = (self.start_time - timedelta(days=1)).date()

        local_evening_before = app_timezone.localize(
            datetime(day_before.year, day_before.month, day_before.day, 17, 0)
        )

        return self.status != 'cancelled' \
            and local_now >= local_evening_before \
            and not any([self.is_ongoing, self.has_passed])


    @property
    def is_ongoing(self):
        if not hasattr(self, '_is_ongoing'):
            self._is_ongoing = self in type(self).current_meeting()

        return self._is_ongoing


    @property
    def has_passed(self):
        return self.start_time < timezone.now() and not self.is_ongoing


    @property
    def ecomment_url(self):
        if self.extras.get('ecomment'):
            return self.extras['ecomment']

        elif self.is_ongoing:
            return self.GENERIC_ECOMMENT_URL


    @property
    def ecomment_message(self):
        if self.status == 'cancelled':
            return

        elif self.has_passed:
            return self.PASSED_ECOMMENT_MESSAGE

        else:
            return self.UPCOMING_ECOMMENT_MESSAGE


class EventAgendaItem(EventAgendaItem):

    class Meta:
        proxy = True

    event = ProxyForeignKey(LAMetroEvent, related_name='agenda', on_delete=models.CASCADE)


class EventRelatedEntity(EventRelatedEntity):

    class Meta:
        proxy = True

    agenda_item = ProxyForeignKey(EventAgendaItem,
                                  related_name='related_entities',
                                  on_delete=models.CASCADE)

    bill = ProxyForeignKey(LAMetroBill, null=True, on_delete=models.SET_NULL)


class LAMetroOrganization(Organization, SourcesMixin):
    '''
    Overrides use the LAMetroEvent object, rather than the default Event
    object, so test events are hidden appropriately.
    '''
    class Meta:
        proxy = True

    @property
    def recent_events(self):
        events = LAMetroEvent.objects.filter(participants__organization=self)
        events = events.order_by('-start_time')
        return events

    @property
    def upcoming_events(self):
        """
        grabs events in the future
        """
        events = LAMetroEvent.objects\
                             .filter(participants__entity_type='organization', participants__entity_name=self.name)\
                             .filter(start_time__gt=datetime.now(app_timezone))\
                             .order_by('start_time')\
                             .all()
        return events


class Membership(councilmatic_core.models.Membership):
    class Meta:
        proxy = True

    organization = ProxyForeignKey(
        LAMetroOrganization,
        related_name='memberships',
        # memberships will go away if the org does
        on_delete=models.CASCADE,
        help_text="A link to the Organization in which the Person is a member."
    )

    person = ProxyForeignKey(
        LAMetroPerson,
        related_name='memberships',
        null=True,
        # Membership will just unlink if the person goes away
        on_delete=models.SET_NULL,
        help_text="A link to the Person that is a member of the Organization."
    )

    post = ProxyForeignKey(
        LAMetroPost,
        related_name='memberships',
        null=True,
        # Membership will just unlink if the post goes away
        on_delete=models.SET_NULL,
        help_text="The Post held by the member in the Organization."
    )


class Packet(models.Model):

    class Meta:
        abstract = True

    updated_at = models.DateTimeField(auto_now=True)
    url = models.URLField()
    ready = models.BooleanField(default=False)

    @property
    def related_entity(self):
        raise NotImplementedError()

    @property
    def related_files(self):
        raise NotImplementedError()

    def save(self, *args, merge=True, **kwargs):
        if merge:
            self._merge_docs()

        self.url = settings.MERGER_BASE_URL + '/document/' + self.related_entity.slug
        super().save(*args, **kwargs)

    def is_ready(self):
        if not self.ready:
            response = requests.head(self.url)
            if response.status_code == 200:
                self.ready = True
                super().save()

        return self.ready

    def _merge_docs(self):
        merge_url = settings.MERGER_BASE_URL + '/merge_pdfs/' + self.related_entity.slug
        requests.post(merge_url, json=self.related_files)


class BillPacket(Packet):

    bill = models.OneToOneField(LAMetroBill,
                                related_name='packet',
                                on_delete=models.CASCADE)

    @property
    def related_entity(self):
        return self.bill

    @property
    def related_files(self):
        board_report = self.bill.versions.get()

        attachments = self.bill.documents\
            .annotate(
                index=Case(
                    When(note__istartswith = '0', then=Value('z')),
                    default=F('note'),
                    output_field=models.CharField()))\
            .order_by('index')

        doc_links = [board_report.links.get().url]

        # sometimes there is more than one url for the same document name
        # https://metro.legistar.com/LegislationDetail.aspx?ID=3104422&GUID=C30D3376-7265-477B-AFFA-815270400538%3e%5d%3e
        # I'm not sure if this a data problem or not, so we'll just
        # add all the doc links
        doc_links += [link.url
                      for doc in attachments
                      for link in doc.links.all()]

        return doc_links


class EventPacket(Packet):

    event = models.OneToOneField(LAMetroEvent,
                                related_name='packet',
                                on_delete=models.CASCADE)

    @property
    def related_entity(self):
        return self.event

    @property
    def related_files(self):

        agenda_doc = self.event.documents.get(note='Agenda')

        related = [agenda_doc.links.get().url]

        agenda_items = self.event.agenda\
            .filter(related_entities__bill__documents__isnull=False)\
            .annotate(int_order=Cast('order', models.IntegerField()))\
            .order_by('int_order')\
            .distinct()

        for item in agenda_items:
            for entity in item.related_entities.filter(bill__isnull=False):
                try:
                    related_bill = entity.bill
                except LAMetroBill.DoesNotExist:
                    # We configure event agenda items to return LAMetroBill
                    # objects. Sometimes, agenda items concern bills that do
                    # not meet criteria for display, e.g., the bill is private
                    # or it does not appear on a published agenda. (See the
                    # LAMetroBill manager for an exhaustive list of display
                    # criteria.) In this case, trying to access the bill via
                    # the event agenda item will raise this exception. Skip
                    # adding those documents to the event packet.
                    continue
                else:
                    bill_packet = BillPacket(bill=related_bill)
                    related.extend(bill_packet.related_files)

        return related


class LAMetroSubject(models.Model):

    CLASSIFICATION_CHOICES = [
        ('bill_type_exact', 'Board Report Type'),
        ('lines_and_ways_exact', 'Lines / Ways'),
        ('phase_exact', 'Phase'),
        ('project_exact', 'Project'),
        ('metro_location_exact', 'Metro Location'),
        ('geo_admin_location_exact', 'Geographic / Administrative Location'),
        ('significant_date_exact', 'Significant Date'),
        ('motion_by_exact', 'Motion By'),
        ('topics_exact', 'Subject'),
    ]

    class Meta:
        unique_together = ['guid', 'name']

    name = models.CharField(max_length=256, unique=True)
    guid = models.CharField(max_length=256, blank=True, null=True)
    classification = models.CharField(
        max_length=256,
        default='topics_exact',
        choices=CLASSIFICATION_CHOICES
    )

    def __str__(self):
        if self.guid is not None:
            return '{0} ({1})'.format(self.name, self.guid)

        else:
            return self.name
