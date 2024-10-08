from octodns.provider.base import BaseProvider
from octodns.record import Record
from collections import defaultdict
import logging
import requests
import time
import json

__version__ = __VERSION__ = '0.0.1'


class DesecAPI():
    API_BASE_URL = 'https://desec.io/api'
    API_DOMAINS_URL = f'{API_BASE_URL}/v1/domains'
    # TODO: add option to customize DEFAULT_*
    # TODO: add customizeable option for maxium waiting time
    DEFAULT_RETRIES = 5
    DEFAULT_INIT_BACKOFF = 2
    def __init__(self, token):
        self.token = token
        self.log = logging.getLogger(f'DesecAPI')
        return

    def _send_request(self, url, method, headers=None, data=None, retries=DEFAULT_RETRIES, backoff=DEFAULT_INIT_BACKOFF, returncode=200):
        # TODO: parse HTTP429 {"detail":"Request was throttled. Expected available in 1 second."}
        if headers is None:
            headers = dict()
        match method.lower():
            case 'get':
                self.log.debug('sending get-request to api')
                r = requests.get(url, headers=headers)
            case 'patch':
                self.log.debug('sending patch-request to api')
                r = requests.patch(url, headers=headers, data=data)
            case _:
                raise Exception('not implemented method')

        if r.status_code != returncode:
            self.log.warning(f'API-Response: {r.content.decode("UTF-8")}')
            if retries > 0:
                self.log.warning(f'API-Statuscode {r.status_code}, expected {returncode} - retry in {backoff} sec')
                time.sleep(backoff)
                r = self._send_request(url=url, method=method, headers=headers, data=data, retries=retries-1, backoff=backoff*2, returncode=returncode)
            else:
                raise Exception('too many API-retries')

        return r

    def get_rrset(self, domainName):
        return_json = []
        url = f'{DesecAPI.API_DOMAINS_URL}/{domainName}/rrsets/?cursor='
        while url != '':
            response = self._send_request(url, method='get', headers={'Authorization': f'Token {self.token}'})
            return_json = return_json + response.json()

            if 'next' in response.links:
                url = response.links['next']['url']
            else:
                url = ''

        return return_json

    def update_rrset(self, domainName, rrset:list):
        self._send_request(f'{DesecAPI.API_DOMAINS_URL}/{domainName}/rrsets/', method='patch', headers={'Authorization': f'Token {self.token}', 'Content-Type': 'application/json'}, data=json.dumps(rrset))


class DesecProvider(BaseProvider):
    SUPPORTS_GEO = False
    SUPPORTS_ROOT_NS = True
    SUPPORTS = {
        'A',
        'AAAA',
        'CAA',
        'CNAME',
        'DS',
        'MX',
        'NS',
        'PTR',
        'SRV',
        'TLSA',
        'TXT',
    }

    def __init__(
        self,
        id,
        token,
        *args,
        **kwargs,
    ):
        self.log = logging.getLogger(f'desecProvider[{id}]')
        self.log.debug(
            '__init__: id=%s',
            id
        )
        self.desec_api = DesecAPI(token)
        self._zone_records = {}

        super().__init__(id)

    def zone_records(self, zone_name):
        # Fetch records from Desec-API that already exist
        records = []

        rrset = self.desec_api.get_rrset(zone_name.name.rstrip('.'))

        for record in rrset:
            for data in record['records']:
                records.append(
                    {
                        'type': record['type'],
                        'name': record['subname'],
                        'ttl': record['ttl'],
                        'data': data
                    }
                )

        return records

    def populate(self, zone, target=False, lenient=False):
        self.log.debug('populate: name=%s, target=%s, lenient=%s', zone.name,
                       target, lenient)

        # fetch data from API and save to values
        values = defaultdict(lambda: defaultdict(list))
        for record in self.zone_records(zone):
            _type = record['type']
            if _type not in self.SUPPORTS:
                continue
            values[record['name']][record['type']].append(record)

        # add data from values to zone.records (octodns)
        before = len(zone.records)
        for name, types in values.items():
            for _type, records in types.items():
                data = getattr(self, f'_data_for_{_type}')(_type, records)
                record = Record.new(zone, name, data,
                                    source=self, lenient=lenient)
                zone.add_record(record, lenient=lenient)

        exists = zone.name in self._zone_records
        self.log.info('populate:   found %s records, exists=%s',
                      len(zone.records) - before, exists)
        return exists
    
    def _apply(self, plan):
        update = []

        for change in plan.changes:

            match change.data['type']:
                case 'delete':
                    update.append(
                        {"subname": change.existing.decoded_name, "type": change.existing.rrs[2], "ttl": '3600', "records": []} # fixed ttl - else if your ttl is 60 for dyndns-records you can not dedlete them
                    )
                case 'create':
                    update.append(
                        {"subname": change.new.decoded_name, "type": change.new.rrs[2], "ttl": change.new.rrs[1], "records": change.new.rrs[3]}
                    )
                case 'update':
                    update.append(
                        {"subname": change.new.decoded_name, "type": change.new.rrs[2], "ttl": change.new.rrs[1], "records": change.new.rrs[3]}
                    )
                case _:
                    raise Exception('not implemented type')

        self.desec_api.update_rrset(plan.desired.decoded_name.rstrip('.'), update)

    def _data_for_multiple(self, _type, records):
        return {
            'ttl': records[0]['ttl'],
            'type': _type,
            'values': [record['data'] for record in records]
        }

    def _data_for_single(self, _type, records):
        return {
            'ttl': records[0]['ttl'],
            'type': _type,
            'value': records[0]['data']
        }

    def _data_for_TXT(self, _type, records):
        return {
            'ttl': records[0]['ttl'],
            'type': _type,
            # escape semicolons
            'values': [record['data'].replace(';', '\\;') for record in records],
        }

    def _data_for_MX(self, _type, records):
        values = []
        for record in records:
            values.append(
                {
                    'preference': record['data'].split(' ')[0],
                    'exchange': record['data'].split(' ')[1],
                }
            )
        return {'ttl': records[0]['ttl'], 'type': _type, 'values': values}

    def _data_for_SRV(self, _type, records):
        values = []
        for record in records:

            values.append(
                {
                    'port': record['data'].split(' ')[2],
                    'priority': record['data'].split(' ')[0],
                    'target': record['data'].split(' ')[3],
                    'weight': record['data'].split(' ')[1],
                }
            )
        return {'type': _type, 'ttl': records[0]['ttl'], 'values': values}

    def _data_for_DS(self, _type, records):
        values = []
        for record in records:
            values.append(
                {
                    'key_tag': record['data'].split(' ')[0],
                    'algorithm': record['data'].split(' ')[1],
                    'digest_type': record['data'].split(' ')[2],
                    'digest': record['data'].split(' ')[3],
                }
            )
        return {'ttl': records[0]['ttl'], 'type': _type, 'values': values}

    def _data_for_CAA(self, _type, records):
        values = []
        for record in records:
            values.append(
                {
                    'flags': record['data'].split(' ')[0].lstrip('"').rstrip('"'),
                    'tag': record['data'].split(' ')[1].lstrip('"').rstrip('"'),
                    'value': record['data'].split(' ')[2].lstrip('"').strip('"'),
                }
            )
        return {'ttl': records[0]['ttl'], 'type': _type, 'values': values}

    def _data_for_TLSA(self, _type, records):
        values = []
        for record in records:
            values.append(
                {
                    'certificate_usage': record['data'].split(' ')[0],
                    'selector': record['data'].split(' ')[1],
                    'matching_type': record['data'].split(' ')[2],
                    'certificate_association_data': record['data'].split(' ')[3],
                }
            )
        return {'ttl': records[0]['ttl'], 'type': _type, 'values': values}

    _data_for_A = _data_for_multiple
    _data_for_AAAA = _data_for_multiple
    _data_for_CNAME = _data_for_single
    _data_for_NS = _data_for_multiple
    _data_for_PTR = _data_for_single
