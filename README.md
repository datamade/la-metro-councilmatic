# Metro Board Reports

[Metro Board Reports](https://boardagendas.metro.net/) helps the Los Angeles community understand the activities of the Los Angeles County Metropolitan Transportation Authority (Metro) – a government agency that consists of several Board Members, who set policy, coordinate, plan, fund, build, and operate transit services and transportation programs throughout LA County.

The Metro Board Reports site monitors all things related to the Metro Board of Directors:

* the board reports introduced and passed
* its various committees and the meetings they hold
* the board members themselves

This site ultimately encourages greater public dialogue and increased awareness about transportation issues in LA County.

Metro Board Reports is a member of the [Councilmatic family](https://www.councilmatic.org/). Learn how to [build your own](https://github.com/datamade/councilmatic-starter-template) Councilmatic site!

## Setup

These days, we run apps in containers for local development. More on that [here](https://github.com/datamade/how-to/blob/master/docker/local-development.md). Prefer to run the app locally? See the [legacy setup instructions](https://github.com/datamade/la-metro-councilmatic/blob/b8bc14f6d90f1b05e24b5076b1bfcd5e0d37527a/README.md).

### Install OS level dependencies:

* [Docker](https://www.docker.com/get-started)

### Run the application

```bash
docker-compose up -d
```

Note that you can omit the `-d` flag to follow the application and service logs. If you prefer a quieter environment, you can view one log stream at a time with `docker-compose logs -f SERVICE_NAME`, where `SERVICE_NAME` is the name of one of the services defined in `docker-compose.yml`, e.g., `app`, `postgres`, etc.

When the command exits (`-d`) or your logs indicate that your app is up and running, visit http://localhost:8000 to visit your shiny, new local application!

### Load in the data

Every hour, DataMade scrapes the Legistar Web API and makes the results available on the Open Civic Data API, which hosts standardized data patterns about government organizations, people, legislation, and events. Metro Board Reports relies upon this data.

To import data, simply run:

```bash
docker-compose run --rm scrapers
```

This may take a few minutes to an hour, depending on the volume of recent
updates.

Once it's finished, head over to http://localhost:8001 to view your shiny new app!

### Optional: Populate the search index

If you wish to use search in your local install, you need a SmartLogic API
key. Initiated DataMade staff may decrypt application secrets for use:

```bash
blackbox_cat configs/settings_deployment.staging.py
```

Grab the `SMARTLOGIC_API_KEY` value from the decrypted settings, and swap it
into your local `councilmatic/settings_deployment.py` file.

Then, run the `refresh_guid` management command to grab the appropriate
classifications for topics in the database.

```bash
python manage.py refresh_guid
```

Finally, add data to your search index with the `update_index` command from
Haystack.


```bash
docker-compose run --rm app python manage.py update_index
```

When the command exits, your search index has been filled. (You can view the
Solr admin panel at http://localhost:8987/solr.)

## Running arbitrary scrapes
Occasionally, after a while without running an event scrape, you may find that your local app is broken. If this happens, make sure you have events in your database that are scheduled for the future, as the app queries for upcoming events in order to render the landing page.

1. Make sure there are future events scheduled in Legistar. Go to the [LA Metro Legistar page](https://metro.legistar.com/Calendar.aspx) and open up the time filter for "All Years".

2. If you notice that there are future events in Legistar, run a small windowed event scrape:

```bash
docker-compose run --rm scrapers pupa update lametro events window=0.05 --rpm=0
```

This will bring in a few upcoming events, and your app will be able to load the landing page.

## Scraping specific bill
It's sometimes helpful to make sure you have a specific bill in your database for debugging. Here's how you can scrape a bill you need:

1. Go to the Legistar Web API at the following URL: http://webapi.legistar.com/v1/metro/matters/?$filter=MatterFile%20eq%20%27<bill_identifier>%27 and find the `<MatterId>` of the bill. The identifier should be in XXXX-XXXX format, and the `MatterId` is a 4 digit number.

2. Run the following command in your shell:
```bash
docker-compose run --rm scrapers pupa update lametro bills matter_ids=<bill_matter_id> --rpm=0
```

## Making changes to the Solr schema

Did you make a change to the schema file that Solr uses to make its magic (`solr_configs/conf/schema.xml`)? Did you add a new field or adjust how Solr indexes data? If so, you need to take a few steps – locally and on the server.

### Local development

First, remove your Solr container.

```bash
# Remove your existing Metro containers and the volume containing your Solr data
docker-compose down
docker volume rm la-metro-councilmatic_lametro-solr-data

# Build the containers anew
docker-compose up -d
```

Then, rebuild your index.

```bash
python manage.py refresh_guid  # Run if you made a change to facets based on topics
docker-compose run --rm app python manage.py rebuild_index --batch-size=50
```

### On the Server

The Dockerized versions of Solr on the server need your attention, too. Perform
the following steps first on staging, then – after confirming that everything
is working as expected – on production.

1. Deploy your changes to the appropriate environment (staging or production).
    - To deploy to staging, merge the relevant PR into `master`.
    - To deploy to production, [create and push a tag](https://github.com/datamade/deploy-a-site/blob/master/How-to-deploy-with-continuous-deployment.md#3-deploy-to-production).

2. Shell into the server, and `cd` into the relevant project directory.
    ```bash
    ssh ubuntu@boardagendas.metro.net

    # Staging project directory: lametro-staging
    # Production project directory: lametro
    cd /home/datamade/${PROJECT_DIRECTORY}
    ```

3. Remove and restart the Solr container.
    ```bash
    # Staging Solr container: lametro-staging-solr
    # Production Solr container: lametro-production-solr
    sudo docker stop ${SOLR_CONTAINER}
    sudo docker rm ${SOLR_CONTAINER}

    sudo docker-compose -f docker-compose.deployment.yml up -d ${SOLR_CONTAINER}
    ```

4. Solr will only apply changes to the schema and config upon core creation, so
consult the Solr logs to confirm the core was remade.
    ```bash
    # Staging Solr service: solr-staging
    # Production Solr service: solr-production
    sudo docker-compose -f docker-compose.deployment.yml logs -f ${SOLR_SERVICE}
    ```

    You should see logs resembling this:

    ```bash
    Attaching to ${SOLR_CONTAINER}
    Executing /opt/docker-solr/scripts/solr-create -c ${SOLR_CORE} -d /la-metro-councilmatic_configs
    Running solr in the background. Logs are in /opt/solr/server/logs
    Waiting up to 180 seconds to see Solr running on port 8983 [\]
    Started Solr server on port 8983 (pid=64). Happy searching!

    Solr is running on http://localhost:8983
    Creating core with: -c ${SOLR_CORE} -d /la-metro-councilmatic_configs
    INFO  - 2020-11-18 13:57:09.874; org.apache.solr.util.configuration.SSLCredentialProviderFactory; Processing SSL Credential Provider chain: env;sysprop

    Created new core '${SOLR_CORE}' <---- IMPORTANT MESSAGE
    Checking core
    ```

    If you see something like "Skipping core creation", you need to perform the
    additional step of recreating the Solr core.

    ```bash
    # Staging Solr core: lametro-staging
    # Production Solr core: lametro
    sudo docker exec ${SOLR_CONTAINER} solr delete -c ${SOLR_CORE}
    sudo docker exec ${SOLR_CONTAINER} solr-create -c ${SOLR_CORE} -d /la-metro-councilmatic_configs
    ```

    Note that we remove and recreate the core, rather than the blunt force
    option of removing the Docker volume containg the Solr data, because the
    staging and production Solr containers use the same volume, so removing it
    would wipe out both indexes at once.

5. Switch to the `datamade` user.
    ```bash
    sudo su - datamade
    ```

6. Rebuild the index:
    ```bash
    # Staging and production virtual environments are named after the corresponding project directory
    source ~/.virtualenvs/${PROJECT_DIRECTORY}/bin/activate
    python manage.py refresh_guid  # Run if you made a change to facets based on topics
    python manage.py rebuild_index --batch-size=50
    ```

Nice! The production server should have the newly edited schema and freshly
built index, ready to search, filter, and facet.

## Adding a new board member

Hooray! A new member has been elected or appointed to the Board of Directors.
Metro will provide a headshot and bio for the new member. There are a few
changes you need to make so they appear correctly on the site.

**N.b., these changes can be made in any order.**

### Update the scraper

- Add the new member to the `VOTING_POSTS` object [in the Metro person scraper](https://github.com/opencivicdata/scrapers-us-municipal/). Once your PR is reviewed, merge your changes, pull them into [our scrapers fork](https://github.com/datamade/scrapers-us-municipal/), and [follow the steps to deploy your change](https://github.com/datamade/scrapers-us-municipal/#deploying-changes).
    - Example: https://github.com/opencivicdata/scrapers-us-municipal/pull/337
    - Tip: Once your scraper change is deployed. Run `docker-compose run --rm scrapers pupa import lametro people --rpm=0` to capture the change locally.
- After the revised person scrape runs, remove any board memberships for the
new member that were created without a post.
    ```python
    from lametro.models import Person
    Person.objects.get(family_name='<MEMBER LAST NAME>').memberships.filter(organization__name='Board of Directors', post__isnull=True).delete()
    ```

### Update the Metro app

- Add the new member's headshot to the `lametro/static/images/manual-headshots`
directory, then update the `MANUAL_HEADSHOTS` object in `councilmatic/settings_jurisdiction.py`. **Be sure to key the headshot off the person slug
from the production site.**
    - Example: https://github.com/datamade/la-metro-councilmatic/pull/686
- Add the new member's bio to the `MEMBER_BIOS` object in `councilmatic/settings_jurisdiction.py`, again **using the person slug from production.**
    - Example: https://github.com/datamade/la-metro-councilmatic/pull/686
    - Tip: Replace newlines in the provided bio with `<br /><br />`.

### Check your work

Since you keyed the changes off the production person slug, they won't be
visible locally or on the staging site. To confirm your changes worked, deploy
them to production, then:

- View [the Board of Directors](https://boardagendas.metro.net/board-members/)
listing and confirm the new member is listed with the correct post, e.g.,
`Los Angeles Country Board Supervisor, District 1`.
    - If you only see `Board Member`, the new member's post has not been added.
    Double check that you updated the `VOTING_POSTS` object in the person
    scraper (e.g., does the member's name as it appears in the API match the
    key you added?), that your changes to the scraper have been deployed, and
    that a person scrape has been run since the deployment.
- View the new member's detail page and confirm that their headshot and bio
appear as expected, and without any formatting issues.

## A note on tests

LA Metro Councilmatic has a basic test suite. If you need to run it, simply run:

```bash
docker-compose -f docker-compose.yml -f tests/docker-compose.yml run --rm app
```

### Load testing

LA Metro Councilmatic uses [Locust](https://docs.locust.io/en/stable/) for load
testing. There is a starter script in `locustfile.py` that visits the homepage,
event listing, and an event detail page at random intervals between 60 and 90
seconds. This script was derived from user behavior in Google Analytics.
(If needed, request analytics access from Metro.)

You can run the load tests using the `locust` service in `docker-compose.locust.yml`:

```bash
docker-compose -f docker-compose.yml -f docker-compose.locust.yml run --service-ports --rm locust
```

This will start the Locust web server on http://localhost:8089. For more details,
see the [Locust documentation](https://docs.locust.io/en/stable/).

## Errors / Bugs

If something is not behaving intuitively, it is a bug, and should be reported.
Report it here: https://github.com/datamade/la-metro-councilmatic/issues

## Note on Patches/Pull Requests

* Fork the project.
* Make your feature addition or bug fix.
* Commit, do not mess with rakefile, version, or history.
* Send a pull request. Bonus points for topic branches.

## Copyright

Copyright (c) 2021 DataMade. Released under the [MIT License](https://github.com/datamade/la-metro-councilmatic/blob/master/LICENSE).
