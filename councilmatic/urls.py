"""councilmatic URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/1.8/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  url(r'^$', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  url(r'^$', Home.as_view(), name='home')
Including another URLconf
    1. Add an import:  from blog import urls as blog_urls
    2. Add a URL to urlpatterns:  url(r'^blog/', include(blog_urls))
"""

from django.conf.urls import include, url
from django.contrib import admin
from django.views.generic.base import RedirectView
from haystack.query import SearchQuerySet
from councilmatic_core.views import CouncilmaticSearchForm, CouncilmaticFacetedSearchView, EventDetailView
from lametro.views import LABillDetail, LABoardMembersView, LAMetroAboutView, LACommitteeDetailView, LAPersonDetailView

sqs = SearchQuerySet().facet('bill_type')\
                      .facet('sponsorships', sort='index')\
                      .facet('controlling_body')\
                      .facet('inferred_status')\
                      .facet('topics')\
                      .facet('legislative_session')\
                      .highlight()

urlpatterns = [
    url(r'^admin/', include(admin.site.urls)),
    url(r'^search/', CouncilmaticFacetedSearchView(searchqueryset=sqs,
                                       form_class=CouncilmaticSearchForm), name='search'),
    url(r'^about/$', LAMetroAboutView.as_view(), name='about'),
    url(r'^legislation/(?P<slug>[^/]+)/$', LABillDetail.as_view(), name='bill_detail'),
    url(r'^committee/(?P<slug>[^/]+)/$', LACommitteeDetailView.as_view(), name='committee'),
    url(r'^board-members/$', LABoardMembersView.as_view(), name='council_members'),
    url(r'^person/(?P<slug>[^/]+)/$', LAPersonDetailView.as_view(), name='person'),
    url(r'^event/(?P<slug>[^/]+)/$', EventDetailView.as_view(), name='event'),
    url(r'', include('councilmatic_core.urls')),
]
