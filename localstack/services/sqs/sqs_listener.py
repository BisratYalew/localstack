import re
import json
import xmltodict
from moto.sqs.utils import parse_message_attributes
from moto.sqs.models import Message, TRANSPORT_TYPE_ENCODINGS
from six.moves.urllib import parse as urlparse
from six.moves.urllib.parse import urlencode
from requests.models import Request, Response
from localstack import config
from localstack.config import HOSTNAME_EXTERNAL, SQS_PORT_EXTERNAL
from localstack.utils.aws import aws_stack
from localstack.utils.common import to_str, clone
from localstack.utils.analytics import event_publisher
from localstack.services.awslambda import lambda_api
from localstack.services.generic_proxy import ProxyListener
from localstack.utils.aws.aws_responses import requests_response

XMLNS_SQS = 'http://queue.amazonaws.com/doc/2012-11-05/'

# list of valid attribute names, and names not supported by the backend (elasticmq)
VALID_ATTRIBUTE_NAMES = ['DelaySeconds', 'MaximumMessageSize', 'MessageRetentionPeriod',
                         'ReceiveMessageWaitTimeSeconds', 'RedrivePolicy', 'VisibilityTimeout',
                         'ContentBasedDeduplication', 'KmsMasterKeyId', 'KmsDataKeyReusePeriodSeconds',
                         'CreatedTimestamp', 'LastModifiedTimestamp', 'FifoQueue',
                         'ApproximateNumberOfMessages', 'ApproximateNumberOfMessagesNotVisible']

UNSUPPORTED_ATTRIBUTE_NAMES = [
    'DelaySeconds', 'MaximumMessageSize', 'MessageRetentionPeriod', 'Policy', 'RedrivePolicy',
    'ContentBasedDeduplication', 'KmsMasterKeyId', 'KmsDataKeyReusePeriodSeconds', 'FifoQueue'
]

# maps queue URLs to attributes set via the API
# TODO: add region as first level in the map
QUEUE_ATTRIBUTES = {}


def parse_request_data(method, path, data):
    """ Extract request data either from query string (for GET) or request body (for POST). """
    if method == 'POST':
        return urlparse.parse_qs(to_str(data))
    elif method == 'GET':
        parsed_path = urlparse.urlparse(path)
        return urlparse.parse_qs(parsed_path.query)
    return {}


# Format attributes as dict. Example input:
#  {
#    'Attribute.1.Name': ['Policy'],
#    'Attribute.1.Value': ['...']
#  }
# TODO still needed?
def format_message_attributes(data):
    prefix = 'MessageAttribute'
    names = []
    for (k, name) in [(k, data[k]) for k in data if k.startswith(prefix) and k.endswith('.Name')]:
        attr_name = name[0]
        k_id = k.split('.')[1]
        names.append((attr_name, k_id))

    msg_attrs = {}
    for (key_name, key_id) in names:
        msg_attrs[key_name] = {}
        # Find vals for each key_id
        attrs = [(k, data[k]) for k in data
                 if k.startswith('{}.{}.'.format(prefix, key_id)) and not k.endswith('.Name')]
        for (attr_k, attr_v) in attrs:
            attr_name = attr_k.split('.')[3]
            msg_attrs[key_name][attr_name[0].lower() + attr_name[1:]] = attr_v[0]

        # These fields are set in the payload sent to Lambda.
        # It is extremely likely additional work will
        # be required to support these fields
        msg_attrs[key_name]['stringListValues'] = []
        msg_attrs[key_name]['binaryListValues'] = []

    return msg_attrs


def _format_attributes(req_data):
    result = {}
    for i in range(1, 500):
        key1 = 'Attribute.%s.Name' % i
        key2 = 'Attribute.%s.Value' % i
        if key1 not in req_data:
            break
        key_name = req_data[key1][0]
        key_value = (req_data.get(key2) or [''])[0]
        result[key_name] = key_value
    return result


# Format attributes as a list. Example input:
#  {
#    'AttributeName.1': ['Policy'],
#    'AttributeName.2': ['MessageRetentionPeriod']
#  }
def _format_attributes_names(req_data):
    result = set()
    for i in range(1, 500):
        key = 'AttributeName.%s' % i
        if key not in req_data:
            break
        result.add(req_data[key][0])
    return result


def _get_attributes_forward_request(method, path, headers, req_data, forward_attrs):
    req_data_new = dict([(k, v) for k, v in req_data.items() if not k.startswith('Attribute.')])
    i = 1
    for k, v in forward_attrs.items():
        req_data_new['Attribute.%s.Name' % i] = [k]
        req_data_new['Attribute.%s.Value' % i] = [v]
        i += 1
    data = urlencode(req_data_new, doseq=True)
    return Request(data=data, headers=headers, method=method)


def _set_queue_attributes(queue_url, req_data):
    attrs = _format_attributes(req_data)
    # select only the attributes in UNSUPPORTED_ATTRIBUTE_NAMES
    local_attrs = {}
    for k, v in attrs.items():
        if k in UNSUPPORTED_ATTRIBUTE_NAMES:
            try:
                _v = json.loads(v)
                if isinstance(_v, dict):
                    if 'maxReceiveCount' in _v:
                        _v['maxReceiveCount'] = int(_v['maxReceiveCount'])

                local_attrs.update(dict({k: json.dumps(_v)}))
            except Exception:
                local_attrs.update(dict({k: v}))

    QUEUE_ATTRIBUTES[queue_url] = QUEUE_ATTRIBUTES.get(queue_url) or {}
    QUEUE_ATTRIBUTES[queue_url].update(local_attrs)
    forward_attrs = dict([(k, v) for k, v in attrs.items() if k not in UNSUPPORTED_ATTRIBUTE_NAMES])
    return forward_attrs


def _fire_event(req_data, response):
    action = req_data.get('Action', [None])[0]
    event_type = None
    queue_url = None
    if action == 'CreateQueue':
        event_type = event_publisher.EVENT_SQS_CREATE_QUEUE
        response_data = xmltodict.parse(response.content)
        if 'CreateQueueResponse' in response_data:
            queue_url = response_data['CreateQueueResponse']['CreateQueueResult']['QueueUrl']
    elif action == 'DeleteQueue':
        event_type = event_publisher.EVENT_SQS_DELETE_QUEUE
        queue_url = req_data.get('QueueUrl', [None])[0]

    if event_type and queue_url:
        event_publisher.fire_event(event_type, payload={'u': event_publisher.get_hash(queue_url)})


def _queue_url(path, req_data, headers):
    queue_url = req_data.get('QueueUrl')
    if queue_url:
        return queue_url[0]
    url = config.TEST_SQS_URL
    if headers.get('Host'):
        url = 'http%s://%s' % ('s' if config.USE_SSL else '', headers['Host'])
    queue_url = '%s%s' % (url, path.partition('?')[0])
    return queue_url


def _add_queue_attributes(path, req_data, content_str, headers):
    flags = re.MULTILINE | re.DOTALL
    queue_url = _queue_url(path, req_data, headers)
    requested_attributes = _format_attributes_names(req_data)
    regex = r'(.*<GetQueueAttributesResult>)(.*)(</GetQueueAttributesResult>.*)'
    attrs = re.sub(regex, r'\2', content_str, flags=flags)
    for key, value in QUEUE_ATTRIBUTES.get(queue_url, {}).items():
        if (not requested_attributes or requested_attributes.intersection({'All', key})) and \
                not re.match(r'<Name>\s*%s\s*</Name>' % key, attrs, flags=flags):
            attrs += '<Attribute><Name>%s</Name><Value>%s</Value></Attribute>' % (key, value)
    content_str = (re.sub(regex, r'\1', content_str, flags=flags) +
                   attrs + re.sub(regex, r'\3', content_str, flags=flags))
    return content_str


def _list_dead_letter_source_queues(queues, queue_url):
    dead_letter_source_queues = []
    for k, v in queues.items():
        for i, j in v.items():
            if i == 'RedrivePolicy':
                f = json.loads(v[i])
                queue_url_split = queue_url.split('/')
                if queue_url_split[-1] in f['deadLetterTargetArn']:
                    dead_letter_source_queues.append(k)
    return format_list_dl_source_queues_response(dead_letter_source_queues)


def format_list_dl_source_queues_response(queues):
    content_str = """<ListDeadLetterSourceQueuesResponse xmlns="{}">
                        <ListDeadLetterSourceQueuesResult>
                        {}
                        </ListDeadLetterSourceQueuesResult>
                    </ListDeadLetterSourceQueuesResponse>"""

    queue_urls = ''
    for q in queues:
        queue_urls += '<QueueUrl>{}</QueueUrl>'.format(q)

    return content_str.format(XMLNS_SQS, queue_urls)

def handle_send_message(queue_url, queue_name, message_body, message_attributes):
        url = 'http://localhost:4576/queue/'
        attr = VALID_ATTRIBUTE_NAMES[-2] 
        q = QUEUE_ATTRIBUTES.get(queue_url)
        if(q and q['RedrivePolicy']):
            rp = json.loads(q['RedrivePolicy'])
            max_msg = rp['maxReceiveCount']
            dl_queue_name = rp['deadLetterTargetArn'].rsplit(':')[-1]
           
            sqs_client = aws_stack.connect_to_service('sqs')
            no_of_msg = int(sqs_client.get_queue_attributes(QueueUrl=queue_url, AttributeNames=[attr])['Attributes'][attr])
            receipt_handle = None
            if no_of_msg: receipt_handle = sqs_client.receive_message(QueueUrl=queue_url, AttributeNames=['ALL'])['Messages'][0]['ReceiptHandle']
            queue_name = dl_queue_name if no_of_msg > max_msg else queue_name
            if no_of_msg > int(max_msg):
                message_attr = { "firstAttribute":{ "DataType":"String","StringValue":"hello world" }, "secondAttribute":{ "DataType":"String","StringValue":"goodbye world"} }
                message_attributes = message_attr
                dl_queue_url = url + dl_queue_name      
                sqs_client.send_message(QueueUrl=dl_queue_url, MessageBody=message_body, MessageAttributes=message_attributes)
                sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
                
                return lambda_api.process_sqs_message(queue_name, message_body, message_attributes)
            
        else:        
            return lambda_api.process_sqs_message(queue_name, message_body, message_attributes)


class ProxyListenerSQS(ProxyListener):
    def forward_request(self, method, path, data, headers):
        if method == 'OPTIONS':
            return 200

        req_data = parse_request_data(method, path, data)

        if req_data:
            action = req_data.get('Action', [None])[0]

            if action == 'SetQueueAttributes':
                queue_url = _queue_url(path, req_data, headers)
                forward_attrs = _set_queue_attributes(queue_url, req_data)
                if len(req_data) != len(forward_attrs):
                    # make sure we only forward the supported attributes to the backend
                    return _get_attributes_forward_request(method, path, headers, req_data, forward_attrs)

            elif action == 'DeleteQueue':
                QUEUE_ATTRIBUTES.pop(_queue_url(path, req_data, headers), None)

            elif action == 'ListDeadLetterSourceQueues':
                queue_url = _queue_url(path, req_data, headers)
                headers = {'content-type': 'application/xhtml+xml'}
                content_str = _list_dead_letter_source_queues(QUEUE_ATTRIBUTES, queue_url)

                return requests_response(content_str, headers=headers)

            if 'QueueName' in req_data:
                encoded_data = urlencode(req_data, doseq=True) if method == 'POST' else ''
                modified_url = None
                if method == 'GET':
                    base_path = path.partition('?')[0]
                    modified_url = '%s?%s' % (base_path, urlencode(req_data, doseq=True))
                return Request(data=encoded_data, url=modified_url, headers=headers, method=method)

        return True

    def return_response(self, method, path, data, headers, response, request_handler):
        if method == 'OPTIONS' and path == '/':
            # Allow CORS preflight requests to succeed.
            return 200

        if method != 'POST':
            return

        region_name = aws_stack.get_region()
        req_data = urlparse.parse_qs(to_str(data))
        action = req_data.get('Action', [None])[0]
        content_str = content_str_original = to_str(response.content)

        if response.status_code >= 400:
            return response

        _fire_event(req_data, response)

        # patch the response and add missing attributes
        if action == 'GetQueueAttributes':
            content_str = _add_queue_attributes(path, req_data, content_str, headers)

        # instruct listeners to fetch new SQS message
        if action == 'SendMessage':
            self._process_sent_message(path, data, req_data, headers)

        # patch the response and return the correct endpoint URLs / ARNs
        if action in ('CreateQueue', 'GetQueueUrl', 'ListQueues', 'GetQueueAttributes'):
            if config.USE_SSL and '<QueueUrl>http://' in content_str:
                # return https://... if we're supposed to use SSL
                content_str = re.sub(r'<QueueUrl>\s*http://', r'<QueueUrl>https://', content_str)
            # expose external hostname:port
            external_port = SQS_PORT_EXTERNAL or get_external_port(headers, request_handler)
            content_str = re.sub(r'<QueueUrl>\s*([a-z]+)://[^<]*:([0-9]+)/([^<]*)\s*</QueueUrl>',
                                 r'<QueueUrl>\1://%s:%s/\3</QueueUrl>' % (HOSTNAME_EXTERNAL, external_port),
                                 content_str)
            # fix queue ARN
            content_str = re.sub(r'<([a-zA-Z0-9]+)>\s*arn:aws:sqs:elasticmq:([^<]+)</([a-zA-Z0-9]+)>',
                                 r'<\1>arn:aws:sqs:%s:\2</\3>' % (region_name), content_str)

            if action == 'CreateQueue':
                queue_url = re.match(r'.*<QueueUrl>(.*)</QueueUrl>', content_str, re.DOTALL).group(1)
                _set_queue_attributes(queue_url, req_data)

        if content_str_original != content_str:
            # if changes have been made, return patched response
            new_response = Response()
            new_response.status_code = response.status_code
            new_response.headers = response.headers
            new_response._content = content_str
            new_response.headers['content-length'] = len(new_response._content)
            return new_response

    # Format of the message Name attribute is MessageAttribute.<int id>.<field>
    # Format of the Value attributes is MessageAttribute.<int id>.Value.DataType
    # and MessageAttribute.<int id>.Value.<Type>Value
    #
    # The data schema changes on transfer between SQS and Lambda (at least)
    # JS functions in real AWS!
    # It is unknown at this time whether this data structure change affects different
    # languages in different ways.
    #
    # The MessageAttributes specified in the SQS payload (in JavaScript):
    # var params = {
    #   MessageBody: "body string",
    #   MessageAttributes: {
    #       "attr_1": {
    #           DataType: "String",
    #           StringValue: "attr_1_value"
    #       },
    #       "attr_2": {
    #           DataType: "String",
    #           StringValue: "attr_2_value"
    #       }
    #   }
    # }
    #
    # The MessageAttributes specified above are massaged into the following structure:
    # {
    #    attr_1: {
    #      stringValue: 'attr_1_value',
    #      stringListValues: [],
    #      binaryListValues: [],
    #      dataType: 'String'
    #    },
    #    attr_2: {
    #      stringValue: 'attr_2_value',
    #      stringListValues: [],
    #      binaryListValues: [],
    #      dataType: 'String'
    #    }
    # }
    # TODO still needed?
    @classmethod
    def get_message_attributes_md5(self, req_data):
        req_data = clone(req_data)
        orig_types = {}
        for key, entry in dict(req_data).items():
            # Fix an issue in moto where data types like 'Number.java.lang.Integer' are
            # not supported: Keep track of the original data type, and temporarily change
            # it to the short form (e.g., 'Number'), before changing it back again.
            if key.endswith('DataType'):
                parts = entry[0].split('.')
                if len(parts) > 2:
                    short_type_name = parts[0]
                    full_type_name = req_data[key][0]
                    attr_num = key.split('.')[1]
                    attr_name = req_data['MessageAttribute.%s.Name' % attr_num][0]
                    orig_types[attr_name] = full_type_name
                    req_data[key] = [short_type_name]
                    if full_type_name not in TRANSPORT_TYPE_ENCODINGS:
                        TRANSPORT_TYPE_ENCODINGS[full_type_name] = TRANSPORT_TYPE_ENCODINGS[short_type_name]
        moto_message = Message('dummy_msg_id', 'dummy_body')
        moto_message.message_attributes = parse_message_attributes(req_data)
        for key, data_type in orig_types.items():
            moto_message.message_attributes[key]['data_type'] = data_type
        message_attr_hash = moto_message.attribute_md5
        return message_attr_hash

    def _process_sent_message(self, path, data, req_data, headers):
        queue_url = _queue_url(path, req_data, headers)
        queue_name = queue_url.rpartition('/')[2]
        message_body = req_data.get('MessageBody', [None])[0]
        message_attributes = format_message_attributes(req_data)
        handle_send_message(queue_url, queue_name, message_body, message_attributes)


# extract the external port used by the client to make the request
def get_external_port(headers, request_handler):
    host = headers.get('Host', '')
    if ':' in host:
        return int(host.split(':')[1])
    # If we cannot find the Host header, then fall back to the port of the proxy.
    # (note that this could be incorrect, e.g., if running in Docker with a host port that
    # is different from the internal container port, but there is not much else we can do.)
    return request_handler.proxy.port


# instantiate listener
UPDATE_SQS = ProxyListenerSQS()
