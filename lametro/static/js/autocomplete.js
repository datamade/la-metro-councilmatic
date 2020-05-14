var SmartLogic = {
  getToken: function() {
    tokenNeeded = !window.localStorage.getItem('ses_token')

    msInDay = 86400000
    tokenExpired = (Date.now() - window.localStorage.getItem('ses_issued')) / msInDay >= 3

    if ( (tokenNeeded || tokenExpired) ) {
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
    return 'https://cloud.smartlogic.com/svc/0ef5d755-1f43-4a7e-8b06-7591bed8d453/ses/CombinedModel/concepts/' + query + '.json?FILTER=AT=System:%20Legistar&stop_cm_after_stage=3&maxResultCount=10';
  }
};

var Autocomplete = {
  getSuggestions: function(request, callback) {
    SmartLogic.getToken()
    $.ajax({
      url: SmartLogic.buildServiceUrl(request.term),
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Authorization': 'Bearer ' + window.localStorage.getItem('ses_token')
      }
    }).then(function(response) {
      var results = Autocomplete.parseSuggestions(response, request.term);
      return callback(results);
    });
  },
  parseSuggestions: function(response, term) {
    var noneFoundText = "No suggestions found. Press enter to perform a keyword search."
    if (response.status_code == 500 || response.terms.length < 1) {
      return [noneFoundText];
    } else {
      return $.map(response.terms, function(d) {
          /* d.term.equivalence is an array of objects. Each object contains
          a "fields" array, also an array of objects, containing alternative
          names for the concept. Identify whether our search term matches one
          of these labels, so we can include it in the suggestion. */
          var nptLabel = '';
          if ( d.term.equivalence !== undefined && d.term.equivalence.length > 0 ) {
            var npt;
            $.each(d.term.equivalence, function(idx, el) {
              npt = el.fields.reduce(function(inp, el) {
                if (inp) {
                  return inp
                } else {
                  if (el.field.name.toLowerCase() == term.toLowerCase()) {
                    return el.field.name;
                  }
                }
              }, undefined);
              if ( npt ) {
                nptLabel = ' (' + npt + ')';
                return false; // Equivalent to "break"
              }
            });
          };
          return {'label': d.term.name + nptLabel, 'value': d.term.id};
      });
    }
  }
}

$.widget( "custom.highlightedAutocomplete", $.ui.autocomplete, {
  _renderItem: function(ul, item) {
    var match = new RegExp(this.term, "i"); // i makes the regex case insensitive
    var highlightedResult = item.label.replace(match, '<strong>' + this.term + '</strong>');

    return $('<li>')
      .attr('data-value', item.value)
      .addClass('autocomplete-suggestion')
      .append('<div>' + highlightedResult + '</div>')
      .appendTo(ul);
  }
});

/* Attaches autocomplete to the element specified by the id passed */
function autocompleteSearchBar(element) {
  $.fn.val = $.fn.html;
  $(element).highlightedAutocomplete({
    source: Autocomplete.getSuggestions
  });
};
