#! /bin/bash

cd /home/datamade/lametro && /home/datamade/.virtualenvs/lametro/bin/python manage.py import_data >> /tmp/lametro-loaddata.log 2>&1
cd /home/datamade/lametro && /home/datamade/.virtualenvs/lametro/bin/python manage.py compile_pdfs >> /tmp/lametro-compilepdfs.log 2>&1
cd /home/datamade/lametro && /home/datamade/.virtualenvs/lametro/bin/python manage.py update_index