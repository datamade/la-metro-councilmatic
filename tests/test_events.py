import pytest
import pytz
from datetime import datetime
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
import requests

from opencivicdata.legislative.models import EventDocument
from councilmatic_core.models import Bill

from lametro.models import LAMetroEvent, app_timezone
from lametro.templatetags.lametro_extras import updates_made
from lametro.forms import AgendaPdfForm


# This collection of tests checks the functionality of Event-specific views, helper functions, and relations.
def test_agenda_creation(event, event_document):
    '''
    Test that the same agenda url does not get added twice.
    '''
    event = event.build()
    agenda = event_document.build()

    agenda, created = EventDocument.objects.get_or_create(event=event)

    assert not created == True


def test_agenda_pdf_form_submit():
    '''
    This unit test checks that a pdf validates the form.
    '''

    with open('tests/test_agenda.pdf', 'rb') as agenda:
        agenda_file = agenda.read()

        agenda_pdf_form = AgendaPdfForm(files={'agenda': SimpleUploadedFile('test_agenda.pdf', agenda_file, content_type='application/pdf')})

        assert agenda_pdf_form.is_valid() == True


def test_agenda_pdf_form_error():
    '''
    This unit test checks that a non-pdf raises an error.
    '''

    with open('tests/test_image.gif', 'rb') as agenda:
        bad_agenda_file = agenda.read()

        agenda_pdf_form = AgendaPdfForm(files={'agenda': SimpleUploadedFile('test_image.gif', bad_agenda_file, content_type='image/gif')})

        assert agenda_pdf_form.is_valid() == False


@pytest.mark.parametrize('has_updates,has_agenda', [
    (True, True),
    (True, False),
    (False, True),
    (False, False),
])
def test_updates_made(event, event_document, mocker, has_updates, has_agenda):
    if has_updates:
        updated_at = LAMetroEvent._time_ago(days=1)
    else:
        updated_at = LAMetroEvent._time_ago(days=7)

    # `updated_at` is an auto_now field, which means that it's always updated to
    # the current date on save. Mock that attribute to return values useful for
    # testing. More on auto_now:
    # https://docs.djangoproject.com/en/3.0/ref/models/fields/#django.db.models.DateField.auto_now
    mock_update = mocker.patch('lametro.models.LAMetroEvent.updated_at', new_callable=mocker.PropertyMock)
    mock_update.return_value = updated_at

    event = event.build(start_date=datetime.now().isoformat()[:25])

    if has_agenda:
        document = event_document.build(note='Agenda')
    else:
        document = event_document.build(note='Some document')

    event.documents.add(document)

    assert updates_made(event.id) == (has_updates and has_agenda)

    if not has_updates:
        # Also test updates after start time
        updated_at = LAMetroEvent._time_from_now(days=3)
        mock_update.return_value = updated_at
        assert updates_made(event.id) == (has_updates and has_agenda)


@pytest.fixture
def concurrent_current_meetings(event):
    '''
    Two meetings scheduled to begin in the next five minutes.
    '''
    board_meeting_info = {
        'id': 'ocd-event/ef33b22d-b166-458f-b254-b81f656ffc09',
        'name': 'Regular Board Meeting',
        'start_date': LAMetroEvent._time_from_now(minutes=3)\
            .replace(second=0, microsecond=0)\
            .isoformat(),
    }
    board_meeting = event.build(**board_meeting_info)

    construction_meeting_info = {
        'id': 'ocd-event/FEC6A621-F5C7-4A88-B2FB-5F6E14FE0E35',
        'name': 'Construction Committee',
        'start_date': LAMetroEvent._time_from_now(minutes=3)\
            .replace(second=0, microsecond=0)\
            .isoformat(),
    }
    construction_meeting = event.build(**construction_meeting_info)

    return board_meeting, construction_meeting


def test_current_meeting_streaming_event(concurrent_current_meetings, mocker):
    '''
    Test that if an event is streaming, it alone is returned as current.
    '''
    dummy_guid = 'a super special guid'

    # Add dummy GUID to one of our events.
    live_meeting, _ = concurrent_current_meetings
    live_meeting.extras = {'guid': dummy_guid.upper()}  # GUIDs in the Legistar API are all caps.
    live_meeting.save()

    # Patch running events endpoint to return our dummy GUID.
    mock_response = mocker.MagicMock(spec=requests.Response)
    mock_response.json.return_value = [dummy_guid]  # GUIDs in running events endpoint are all lowercase.

    mocker.patch('lametro.models.requests.get', return_value=mock_response)

    current_meetings = LAMetroEvent.current_meeting()

    # Assert that we returned the streaming meeting.
    assert current_meetings.get() == live_meeting


def test_current_meeting_no_streaming_event(concurrent_current_meetings,
                                            mocker):
    '''
    Test that if an event is not streaming, and there are concurrently
    scheduled events, both events are returned as current.
    '''
    # Patch running events endpoint to return no running events.
    mock_response = mocker.MagicMock(spec=requests.Response)
    mock_response.json.return_value = []

    mocker.patch('lametro.models.requests.get', return_value=mock_response)

    current_meetings = LAMetroEvent.current_meeting()

    # Test that the board meeting is returned first.
    assert current_meetings.first().name == 'Regular Board Meeting'

    # Test that both meetings are returned.
    assert all(m in current_meetings for m in concurrent_current_meetings)


def test_current_meeting_no_streaming_event_late_start(event, mocker):
    '''
    Test that if an meeting is scheduled but not yet streaming, it is returned
    as current up to 20 minutes past its scheduled start.
    '''
    # Build an event scheduled to start 15 minutes ago.
    crenshaw_meeting_info = {
        'id': 'ocd-event/3c93e81f-f1a9-42ce-97fe-30c77a4a6740',
        'name': 'Crenshaw Project Corporation',
        'start_date': LAMetroEvent._time_ago(minutes=15)\
            .replace(second=0, microsecond=0)\
            .isoformat(),
    }
    late_current_meeting = event.build(**crenshaw_meeting_info)

    # Patch running events endpoint to return no running events.
    mock_response = mocker.MagicMock(spec=requests.Response)
    mock_response.json.return_value = []

    mocker.patch('lametro.models.requests.get', return_value=mock_response)

    current_meetings = LAMetroEvent.current_meeting()

    # Assert that we returned the late meeting.
    assert current_meetings.get() == late_current_meeting


def test_current_meeting_no_potentially_current(event):
    '''
    Test that if there are no potentially current meetings (scheduled to
    start in the last six hours, or in the next five minutes), no meetings
    are returned as current.
    '''
    # Build an event outside of the "potentially current" timeframe.
    safety_meeting_info = {
        'id': 'ocd-event/5e84e91d-279c-4c83-a463-4a0e05784b62',
        'name': 'System Safety, Security and Operations Committee',
        'start_date': LAMetroEvent._time_from_now(hours=12)\
            .replace(second=0, microsecond=0)\
            .isoformat(),
    }
    event.build(**safety_meeting_info)

    current_meetings = LAMetroEvent.current_meeting()

    # Assert we did not return any current meetings.
    assert not current_meetings


def test_upcoming_board_meetings(event):
    one_minute_from_now = LAMetroEvent._time_from_now(minutes=1).strftime('%Y-%m-%d %H:%M')
    forty_days_ago = LAMetroEvent._time_ago(days=40).strftime('%Y-%m-%d %H:%M')
    forty_days_from_now = LAMetroEvent._time_from_now(days=40).strftime('%Y-%m-%d %H:%M')

    def get_event_id():
        return 'ocd-event/{}'.format(str(uuid4()))

    # Create a past meeting
    past_board_meeting = event.build(
        name='Regular Board Meeting',
        start_date=forty_days_ago,
        id=get_event_id()
    )

    # Create some meetings for the current date, i.e., upcoming meetings
    upcoming_board_meeting = event.build(
        name='Regular Board Meeting',
        start_date=one_minute_from_now,
        id=get_event_id()
    )
    upcoming_special_board_meeting = event.build(
        name='Special Board Meeting',
        start_date=one_minute_from_now,
        id=get_event_id()
    )
    upcoming_committee_meeting = event.build(
        name='Committee Meeting',
        start_date=one_minute_from_now,
        id=get_event_id()
    )

    # Create a future meeting
    future_board_meeting = event.build(
        name='Regular Board Meeting',
        start_date=forty_days_from_now,
        id=get_event_id()
    )

    upcoming_meetings = LAMetroEvent.upcoming_board_meetings()

    assert upcoming_meetings.count() == 2

    for meeting in (upcoming_board_meeting, upcoming_special_board_meeting):
        assert meeting in upcoming_meetings

    for meeting in (past_board_meeting, upcoming_committee_meeting, future_board_meeting):
        assert meeting not in upcoming_meetings


def _test_redirect(response, expected_location):
    redirect, = response.redirect_chain

    redirect_url, redirect_status_code = redirect

    assert redirect_status_code == 302
    assert redirect_url == expected_location


@pytest.mark.django_db
def test_public_comment_endpoint_no_meeting(client):
    response = client.get(reverse('lametro:public_comment'), follow=True)
    _test_redirect(response, LAMetroEvent.GENERIC_ECOMMENT_URL)


@pytest.mark.django_db
def test_public_comment_endpoint_one_meeting(concurrent_current_meetings, mocker, client):
    dummy_guid = 'a super special guid'
    ecomment_url = 'https://ecomment.url'

    # Add dummy GUID to one of our events.
    live_meeting, _ = concurrent_current_meetings

    live_meeting.extras = {
        'guid': dummy_guid.upper(),  # GUIDs in the Legistar API are all caps.
        'ecomment': ecomment_url,
    }

    live_meeting.save()

    # Patch running events endpoint to return our dummy GUID.
    mock_response = mocker.MagicMock(spec=requests.Response)
    mock_response.json.return_value = [dummy_guid]  # GUIDs in running events endpoint are all lowercase.

    mocker.patch('lametro.models.requests.get', return_value=mock_response)

    response = client.get(reverse('lametro:public_comment'), follow=True)
    _test_redirect(response, ecomment_url)


@pytest.mark.django_db
def test_public_comment_endpoint_concurrent_meetings(concurrent_current_meetings, client):
    response = client.get(reverse('lametro:public_comment'), follow=True)
    _test_redirect(response, LAMetroEvent.GENERIC_ECOMMENT_URL)
