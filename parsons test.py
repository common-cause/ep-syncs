from parsons import GoogleBigQuery as BigQuery, Table
from parsons import GoogleSheets
from petl import select
from parsons.google.google_bigquery import map_column_headers_to_schema_field
import requests
bq = BigQuery(app_creds='com-tmc.json',project='proj-tmc-mem-com')
import csv
import json

gcs_temp_bucket = "bkt-tmc-mem-com-scratch"
shifting_table_schema = [
    {"name": "shift_id", "field_type": "INTEGER"},
    {"name": "inserted_at", "field_type": "STRING"},
    {"name": "date", "field_type": "DATE"},
    {"name": "start_time", "field_type": "TIME"},
    {"name": "end_time", "field_type": "TIME"},
    {"name": "timezone", "field_type": "STRING"},
    {"name": "locations", "field_type": "STRING"},
    {"name": "county", "field_type": "STRING"},
    {"name": "first_name", "field_type": "STRING"},
    {"name": "last_name", "field_type": "STRING"},
    {"name": "phone_number", "field_type": "STRING"},
    {"name": "email", "field_type": "STRING"},
    {"name": "role", "field_type": "STRING"},
    {"name": "source", "field_type": "STRING"},
    {"name": "state", "field_type": "STRING"}]

all_users_schema = [
    {"name": "id", "field_type": "INTEGER"},
    {"name": "email", "field_type": "STRING"},
    {'name' : 'join_date', 'field_type' : 'TIMESTAMP'},
    {"name": "phone_number", "field_type": "STRING"},
    {"name": "first_name", "field_type": "STRING"},
    {"name": "last_name", "field_type": "STRING"},
    {"name": "county", "field_type": "STRING"},
    {'name' :'zip_code', 'field_type' : 'STRING'},
    {'name' :'source_code', 'field_type' : 'STRING'},
    {'name' :'regional_admin', 'field_type' : 'STRING'},
    {'name' :'shifted', 'field_type' : 'STRING'},
    {'name' :'training', 'field_type' : 'STRING'},
    {'name' :'role', 'field_type' : 'STRING'},
    {'name' :'state', 'field_type' : 'STRING'}]

all_shifts_schema = map_column_headers_to_schema_field([
    {"name": "id", "field_type": "INTEGER"},
    {"name" : "date", "field_type" : "DATE"},
    {"name": "start_time", "field_type": "TIME"},
    {"name": "end_time", "field_type": "TIME"},
    {"name": "locations_string", "field_type": "STRING"},
    {"name" : "volunteers", "field_type" : "INTEGER"},
    {"name" : "filled", "field_type" : "INTEGER"},
    {"name" : "state", "field_type" : "STRING"}])

credentials = json.load(open('com-tmc.json'))
sheets = GoogleSheets(google_keyfile_dict=credentials)
                    

with open('states.csv','rt') as src:
    c = csv.DictReader(src)
    states = [row['Abbreviation'] for row in c]

ptv_key = os.environ['PTV_API_KEY_PASSWORD']
shift_endpoint = 'https://app.protectthevote.net/api/shift_volunteers_csv'
all_users_endpoint = 'https://app.protectthevote.net/api/users_csv'
all_shift_endpoint =  'https://app.protectthevote.net/api/state_shifts_csv'

def _download(state, endpoint):
    r = requests.get(endpoint,params={'key' : ptv_key, 'state_code' : state},auth=('colab',ptv_key))
    table = Table.from_csv_string(r.text,encoding='utf-8')
    table.add_column("state",value=state)
    return table

def download_state(state):
    state_table = _download(state,all_users_endpoint)    
    return state_table

def download_state_shifts(state):
    state_table = _download(state,shift_endpoint)
    return state_table

def download_state_allshifts(state):
    state_table = _download(state,all_shift_endpoint)
    return state_table

def _upload(table,dest_table,schema):
    bq.copy(table,
            f"sheets_exports.{dest_table}",
            if_exists='truncate',
            tmp_gcs_bucket=gcs_temp_bucket,
            schema=schema)

def upload_allusers(table,dest_table):
    _upload(table,dest_table,all_users_schema)

def upload_shifts(table,dest_table):
    _upload(table,dest_table,shifting_table_schema)

def upload_allshifts(table,dest_table):
    _upload(table,dest_table,all_shifts_schema)
    
comprehensive_allusers = Table()
comprehensive_shifts = Table()

def load_all():
    for state in states:
        print('working on state %s' % state)
        d = download_state(state)
        comprehensive_allusers.concat(d)
        if d.num_rows > 0:
            upload_allusers(d,'state25_'+state)
        s = download_state_shifts(state)
        comprehensive_shifts.concat(s)
        if s.num_rows > 0:
            upload_shifts(s,'state_shifts25_'+state)
        sh = download_state_allshifts(state)
        if sh.num_rows > 0:
            upload_allshifts(sh,'all_shifts25_'+state)
    unique_sources = comprehensive_allusers.table.distinct(key='source_code').values('source_code')
    unique_sources.remove('bpep)')
    for source in unique_sources:
        print('working on source %s' % source)
        source_table = comprehensive_allusers.select_rows(lambda rec: rec.source_code == source)
        upload_allusers(source_table,'source25_'+source)
    upload_allusers(comprehensive_allusers,'all25_volunteers')
    try:
        comprehensive_shifts.remove_column('{"errors":{"detail":"Not Found"}}')
    except:
        pass
    upload_shifts(comprehensive_shifts,'all25_shifts')

if __name__ == '__main__':
    load_all()


