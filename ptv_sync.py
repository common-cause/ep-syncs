Python 3.12.5 (tags/v3.12.5:ff3bc82, Aug  6 2024, 20:45:27) [MSC v.1940 64 bit (AMD64)] on win32
Type "help", "copyright", "credits" or "license()" for more information.
>>> # Syncing in data from Common Causes own shifting tool
... # Civis Link: https://platform.civisanalytics.com/spa/#/scripts/containers/261090891
... 
... 
... from parsons import GoogleBigQuery as BigQuery, Table
... from canalespy import logger, setup_environment
... from bigquery.src.credential_helpers import (
...     set_google_credential_to_member_service_account,
... )
... import os
... import requests
... 
... def set_google_credential_to_member_service_account(
...     member_code=None, project="proj-tech-sandbox"
... ):
...     """A function to fetch BigQuery project credentials from 1password.
... 
...     Args:
...         member_code: str
...         The member code associated with the member bigquery project you wish to interact with.
...         If not specified, the project arg will be used
...         project: str
...         A bigquery project you wish to interact with. Accepted values are proj-tech-sandbox,
...         tmc-dev-394022, and tmc-data-assets
...     Returns:
...         None
...     """
    # If we are running in Civis, we don't need to connect to 1Password
    if not os.getenv("CIVIS_RUN_ID"):
        if member_code:
            uri = get_uri_for_member_service_account(member_code)
        else:
            uri = get_uri_for_member_service_account(project=project)

        set_cred_in_env(uri=uri)


def get_uri_for_member_service_account(member_code=None, project="proj-tech-sandbox"):
    if member_code:
        member_code = member_code.lower()
        return f"op://Member GCP Service Accounts/{member_code} GCP Service Account JSON/{member_code}_gcp_service_account_json.json"
    elif project == "tmc-dev-394022":
        return "op://Engineering Team/sd2alxo57xp2j6nnxiqfus7lqi/tmc-dev-load.json"
    elif project == "tmc-data-assets":
        "op://Shared Tools/TMC Data Assets GCP Service Account JSON/tmc-data-assets-d41f3325c4e9.json"
    elif project == "proj-tech-sandbox":
        return "op://Shared Tools/GCP Sandbox JSON Credential/proj-tech-sandbox-b4b4088ec030.json"


def concat_tables(tables):
    """
    Description:
        Loop through a list of Parsons tables, concat them into one Parsons table.

    Args:
        tables: list
            a list of Parsons tables
    Returns:
        Parsons table
    """
    concated_table = Table()
    i = 0
    logger.info(f"{len(tables)} tables to concat...")

    # Append to the full table and materialize to avoid for loop weirdness
    for table in tables:
        i += 1
        logger.info(f"working on table {i}...")
        concated_table.concat(table)
        concated_table.materialize()
    return concated_table


def main(bq):

    # getting the states from the mapping table
    state_query = db.query(
        "select distinct state from ep.ep_shifting_to_airtable_base_mapping_2024"
    )
    # looping through every state and making the API call for that state and adding that state table
    # to a list of tables
    table_list = []
    for row in state_query:
        state = row["state"]
        shifts_url = f"https://app.protectthevote.net/api/shift_volunteers_csv?key={pw}&state_code={state}"
        s = requests.get(shifts_url, auth=(username, pw))
        shifting_text = s.text
        state_table = Table.from_csv_string(shifting_text)
        state_table.add_column("state", value=f"{state}")
        logger.info(f"Adding {state_table.num_rows} signups from {state}...")
        table_list.append(state_table)

    # concatenating all the state tables into one big table
    all_states_tables = concat_tables(table_list)

    # copying to BigQuery
    logger.info(
        f"Adding {all_states_tables.num_rows} rows of signup data to {schema}.{table}...!"
    )

    if all_states_tables.num_rows > 0:
        db.copy(
            all_states_tables,
            f"{schema}.{table}",
            if_exists="drop",
            tmp_gcs_bucket=gcs_temp_bucket,
            schema=shifting_table_schema,
        )

    else:
        logger.info("No data to copy!")


if __name__ == "__main__":
    setup_environment()
    db = BigQuery()

    username = os.environ["CC_SHIFTINGTOOL_APIKEY_USERNAME"]  # 'colab'
    pw = os.environ["CC_SHIFTINGTOOL_APIKEY_PASSWORD"]
    schema = os.environ["SCHEMA"]
    table = os.environ["TABLE"]
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
        {"name": "state", "field_type": "STRING"},
    ]

