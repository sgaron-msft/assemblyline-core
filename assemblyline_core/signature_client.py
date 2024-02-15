import hashlib
import logging
import yaml

from assemblyline.common import forge
from assemblyline.common.isotime import iso_to_epoch
from assemblyline.datastore.helper import AssemblylineDatastore
from assemblyline.odm.messages.changes import Operation
from assemblyline.odm.models.user import ROLES
from assemblyline.remote.datatypes.lock import Lock

from assemblyline_ui.config import SERVICE_LIST
from assemblyline.odm.models.service import SIGNATURE_DELIMITERS
from assemblyline.common.memory_zip import InMemoryZip


def _get_signature_delimiters():
    signature_delimiters = {}
    for service in SERVICE_LIST:
        if service.get("update_config", {}).get("generates_signatures", False):
            signature_delimiters[service['name'].lower()] = _get_signature_delimiter(service['update_config'])
    return signature_delimiters


def _get_signature_delimiter(update_config):
    delimiter_type = update_config['signature_delimiter']
    if delimiter_type == 'custom':
        delimiter = update_config['custom_delimiter'].encode().decode('unicode-escape')
    else:
        delimiter = SIGNATURE_DELIMITERS.get(delimiter_type, '\n\n')
    return {'type': delimiter_type, 'delimiter': delimiter}


DEFAULT_DELIMITER = "\n\n"
DELIMITERS = forge.CachedObject(_get_signature_delimiters)
CLASSIFICATION = forge.get_classification()


# Signature class
class SignatureClient:
    """A helper class to simplify signature management for privileged services and service-server."""

    def __init__(self, datastore: AssemblylineDatastore = None, config=None):
        self.log = logging.getLogger('assemblyline.signature_client')
        self.config = config or forge.CachedObject(forge.get_config)
        self.datastore = datastore or forge.get_datastore(self.config)

    def add_update(self, data, dedup_name=True):
        if data.get('type', None) is None or data['name'] is None or data['data'] is None:
            raise ValueError("Signature id, name, type and data are mandatory fields.")

        # Compute signature ID if missing
        data['signature_id'] = data.get('signature_id', data['name'])

        key = f"{data['type']}_{data['source']}_{data['signature_id']}"

        # Test signature name
        if dedup_name:
            check_name_query = f"name:\"{data['name']}\" " \
                f"AND type:\"{data['type']}\" " \
                f"AND source:\"{data['source']}\" " \
                f"AND NOT id:\"{key}\""
            other = self.datastore.signature.search(check_name_query, fl='id', rows='0')
            if other['total'] > 0:
                raise ValueError("A signature with that name already exists")

        old = self.datastore.signature.get(key, as_obj=False)
        op = Operation.Modified if old else Operation.Added
        if old:
            if old['data'] == data['data']:
                return True, key, None

            # Ensure that the last state change, if any, was made by a user and not a system account.
            user_modified_last_state = old['state_change_user'] not in ['update_service_account', None]

            # If rule state is moving to an active state but was disabled by a user before:
            # Keep original inactive state, a user changed the state for a reason
            if user_modified_last_state and data['status'] == 'DEPLOYED' and data['status'] != old['status']:
                data['status'] = old['status']

            # Preserve last state change
            data['state_change_date'] = old['state_change_date']
            data['state_change_user'] = old['state_change_user']

            # Preserve signature stats
            data['stats'] = old['stats']

        # Save the signature
        success = self.datastore.signature.save(key, data)
        return success, key, op

    def add_update_many(self, source, sig_type, data, dedup_name=True):
        if source is None or sig_type is None or not isinstance(data, list):
            raise ValueError("Source, source type and data are mandatory fields.")

        # Test signature names
        names_map = {x['name']: f"{x['type']}_{x['source']}_{x.get('signature_id', x['name'])}" for x in data}

        skip_list = []
        if dedup_name:
            for item in self.datastore.signature.stream_search(f"type: \"{sig_type}\" AND source:\"{source}\"",
                                                               fl="id,name", as_obj=False, item_buffer_size=1000):
                lookup_id = names_map.get(item['name'], None)
                if lookup_id and lookup_id != item['id']:
                    skip_list.append(lookup_id)

            if skip_list:
                data = [
                    x for x in data
                    if f"{x['type']}_{x['source']}_{x.get('signature_id', x['name'])}" not in skip_list]

        old_data = self.datastore.signature.multiget(list(names_map.values()), as_dictionary=True, as_obj=False,
                                                     error_on_missing=False)

        plan = self.datastore.signature.get_bulk_plan()
        for rule in data:
            key = f"{rule['type']}_{rule['source']}_{rule.get('signature_id', rule['name'])}"
            if key in old_data:
                # Ensure that the last state change, if any, was made by a user and not a system account.
                user_modified_last_state = old_data[key]['state_change_user'] not in ['update_service_account', None]

                # If rule state is moving to an active state but was disabled by a user before:
                # Keep original inactive state, a user changed the state for a reason
                if user_modified_last_state and rule['status'] == 'DEPLOYED' and rule['status'] != old_data[key][
                        'status']:
                    rule['status'] = old_data[key]['status']

                # Preserve last state change
                rule['state_change_date'] = old_data[key]['state_change_date']
                rule['state_change_user'] = old_data[key]['state_change_user']

                # Preserve signature stats
                rule['stats'] = old_data[key]['stats']

            plan.add_upsert_operation(key, rule)

        if not plan.empty:
            res = self.datastore.signature.bulk(plan)
            return {"success": len(res['items']), "errors": res['errors'], "skipped": skip_list}

        return {"success": 0, "errors": [], "skipped": skip_list}

    def download(self, query=None, access=None) -> bytes:
        if not query:
            query = "*"

        output_files = {}

        signature_list = sorted(
            self.datastore.signature.stream_search(
                query, fl="signature_id,type,source,data,order", access_control=access, as_obj=False,
                item_buffer_size=1000),
            key=lambda x: x['order'])

        for sig in signature_list:
            out_fname = f"{sig['type']}/{sig['source']}"
            if DELIMITERS.get(sig['type'], {}).get('type', None) == 'file':
                out_fname = f"{out_fname}/{sig['signature_id']}"
            output_files.setdefault(out_fname, [])
            output_files[out_fname].append(sig['data'])

        output_zip = InMemoryZip()
        for fname, data in output_files.items():
            separator = DELIMITERS.get(fname.split('/')[0], {}).get('delimiter', DEFAULT_DELIMITER)
            output_zip.append(fname, separator.join(data))

        return output_zip.read()

    def update_available(self, since='', sig_type='*'):
        since = since or '1970-01-01T00:00:00.000000Z'
        last_update = iso_to_epoch(since)
        last_modified = iso_to_epoch(self.datastore.get_signature_last_modified(sig_type))

        return last_modified > last_update
