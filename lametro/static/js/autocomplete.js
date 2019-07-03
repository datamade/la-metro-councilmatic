var SmartLogic = {
  getToken: function(refresh=false) {
    tokenNeeded = !window.localStorage.getItem('ses_token')

    msInDay = 86400000
    tokenExpired = (Date.now() - window.localStorage.getItem('ses_issued')) / msInDay >= 14

    if ( refresh || (tokenNeeded || tokenExpired) ) {
      return $.get(
        '/ses-token/'
      ).then(function(response) {
          window.localStorage.setItem('ses_token', response.access_token);
          window.localStorage.setItem('ses_issued', Date.now());
      }).fail(function() {
          console.log('Failed to retrieve token');
      });
    };
  },
  buildServiceUrl: function(query) {
    return 'https://cloud.smartlogic.com/svc/0ef5d755-1f43-4a7e-8b06-7591bed8d453/ses/CombinedModel/hints/' + query + '.json?FILTER=AT=System:%20Legistar';
  }
};

/* Attaches autocomplete to the input div specified by the id passed */
function autocompleteSearchBar(element) {
  $(element).autocomplete({
    serviceUrl: SmartLogic.buildServiceUrl,
    ajaxSettings: {
      beforeSend: function (xhr) {
        SmartLogic.getToken();
        xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
        xhr.setRequestHeader('Authorization', 'Bearer ' + window.localStorage.getItem('ses_token'));
      },
      data: {}
    },
    paramName: '',
    transformResult: function(response) {
      var res = JSON.parse(response);
      if (res.status_code == 500 || res.termHints.length < 1) {
        return {
          suggestions: ['No topics found']
        };
      } else {
        return {
          suggestions: $.map(res.termHints, function(d) {
              /* If the search term is an acronym, displays that as a part of the suggestion */
              var nature = '';
              if (d.values[0]['nature'] == 'NPT') {
                nature = d.values[0]['value'];
                nature = ' (' + nature + ')';
              }
              return {'value': d.name + nature, 'data': d.id};
            }
          )
        };
      }
    },
    onSelect: function(suggestion) {
      $.when(
        $.get('/topic/', { 'guid': suggestion.data })
      ).then(function(response) {
        if (response.status_code == 200) {
          var base = '/search/?q=';
          var url = base + response.subject_safe;
          window.location.href = url;
        };
      });
    },
    deferRequestBy: 100
  });
};
